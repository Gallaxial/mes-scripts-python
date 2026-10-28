#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dumpbox.py — Boite de depot (DUMP BOX), en Python.

Un petit serveur dedie, independant des autres : il affiche un
formulaire d'envoi et recoit des fichiers archives dans un dossier
pre-etabli. Rien d'autre : aucune liste, aucun telechargement, les
fichiers recus ne sont jamais servis en public.

Reglages dans dumpbox-config.conf, a cote du script (cree au premier
demarrage si absent) : PORT, DOSSIER_RECEPTION, TAILLE_MAX_MO,
NB_MAX_FICHIERS, VITESSE_MAX_MO_S, EXTENSIONS, PURGE_JOURS. Le dossier de reception est cree s'il n'existe pas.

Protections :
  - extensions acceptees seulement (.zip, .rar, .7z par defaut) ;
    .exe, .com, .bat, .scr et tout le reste sont refuses ;
  - verification magique : le debut du fichier doit correspondre a
    une vraie archive (un .exe renomme .zip est refuse) ;
  - taille max par fichier, verifiee avant lecture du corps ;
  - noms de fichiers nettoyes (pas de chemin, pas de ".."), doublons
    renommes " (2)", " (3)" ;
  - aucune extraction automatique, aucun fichier execute : le depot
    reste tel quel dans le dossier, a toi de l'ouvrir.

v1.1 : ecriture en continu (streaming). Le fichier s'ecrit sur le
disque par morceaux de 1 Mo au fur et a mesure, au lieu d'etre
charge en entier en memoire : la limite devient l'espace disque,
plus la RAM. Une ligne de progression se met a jour dans la
fenetre du .bat pendant le transfert.

v1.2 : plusieurs fichiers en un seul envoi (champ multiple). Chaque
fichier est verifie separement (extension, magie, taille) ; la page
finale recapitule : "Fichiers recus et verifies" et les refuses avec
leur raison. Un fichier deja recu reste recu meme si un suivant
echoue — pratique pour les RAR en parties et les connexions lentes.

v1.3 : envoi fichier par fichier pilote par la page (JavaScript).
La page affiche la liste choisie (avec retrait un par un et tout
effacer), le total avant l'envoi, puis pendant l'envoi le compteur
(fichier i/N), la progression par fichier et totale, et un bouton
Annuler. A la fin — meme en cas d'annulation — un recapitulatif
montre les termines et les non termines. La limite de fichiers par
envoi (NB_MAX_FICHIERS, config) est appliquee par la page, et aussi
par le serveur pour les envois classiques. Les fichiers partiels
(.tmp-) sont effaces en cas d'interruption, et ceux restants d'un
arret brutal sont nettoyes au demarrage. Correctif : un nom tres
long n'est plus coupe au milieu de son extension — la troncature
garde l'extension (.rar/.zip) intacte, sinon le fichier etait refuse
a tort. Le journal inscrit chaque fichier des qu'il est decide, pour
qu'une coupure ne fasse plus perdre la trace des fichiers recus.

v1.4 : les x de retrait de la liste sont a gauche du nom, et plus
petits.

v1.5 : la liste est alignee a gauche : tous les x forment une
meme colonne, plus facile a lire que des lignes centrees.

v1.6 : fenetre elargie (640 a 900 px, toujours limitee par le
navigateur) et retour a la ligne en retrait : quand un nom depasse,
la ligne suivante s'aligne sous le debut du nom, pas sous le x.

v1.7 : vitesse max d'envoi (VITESSE_MAX_MO_S, config). En Mo/s
decimaux : 1 Mo/s = 1 000 000 octets/s, pas 1 048 576. 0 = illimite.
La limite s'applique par envoi (un flux a la fois).

v1.8 : le plafond devient global et partage : un seul limiteur a
jetons pour tout le serveur, donc 30 Mo/s reste 30 Mo/s au total
meme si deux personnes envoient en meme temps (avant, chaque envoi
avait droit a 30 Mo/s). Le plafond ne s'accumule pas pendant les
periodes calmes.

v1.9 : la ligne de progression dans la fenetre du .bat affiche
aussi la vitesse moyenne en direct, en Mo/s decimaux (meme unite
que VITESSE_MAX_MO_S) : on voit tout de suite si la limite mord.

v1.10 : la page web affiche aussi la vitesse en direct, sur la
ligne de progression de chaque fichier (meme unite, Mo/s decimaux).

v1.11 : purge du journal au demarrage (PURGE_JOURS, defaut 30) :
log/dumpbox.log ne garde que les lignes des 30 derniers jours.
Les lignes sans date lisible sont effacees.
"""

# ==================== VERSION ====================
# Numero de version du script (MAJEUR.MINEUR) — affiche dans la
# console au demarrage. Meme regle que les autres serveurs :
#   - changement MAJEUR (nouvelle fonction, changement de comportement)
#     -> MAJEUR + 1, MINEUR remis a 0  (ex. 1.0 -> 2.0)
#   - petit changement (correctif, texte, reglage)
#     -> MINEUR + 1  (ex. 1.0 -> 1.1)
# VERSION = "1.11"
VERSION = "1.11"

import html
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "dumpbox-config.conf")
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass
LOG_FILE = os.path.join(LOG_DIR, "dumpbox.log")

DEFAUTS = {
    "PORT": 8083,
    "DOSSIER_RECEPTION": "D:/UPL",
    "TAILLE_MAX_MO": 2048,
    "EXTENSIONS": "zip,rar,7z",
    "NB_MAX_FICHIERS": 20,
    "VITESSE_MAX_MO_S": 0,
    "PURGE_JOURS": 30,
}

MODELE_CONFIG = """# dumpbox-config.conf — Boite de depot (DUMP BOX).
# Une ligne = un reglage NOM = valeur. Les lignes # sont ignorees.
# PORT : port d'ecoute (8083 par defaut).
# DOSSIER_RECEPTION : ou les fichiers recus sont deposes. Ce dossier
#   n'est jamais servi en public : les depots n'existent que pour toi.
# TAILLE_MAX_MO : taille max par fichier, en Mo.
# EXTENSIONS : archives acceptees, separees par des virgules.
# NB_MAX_FICHIERS : nombre max de fichiers par envoi (la page
#   refuse une selection plus grande ; le serveur ignore les
#   fichiers au-dela pour les envois classiques).
# VITESSE_MAX_MO_S : vitesse max d'envoi, en Mo/s DECIMAUX
#   (1 Mo/s = 1 000 000 octets/s, pas 1 048 576). 0 = illimite.
#   Plafond GLOBAL : 30 = 30 Mo/s max au total, tous envois confondus.
# PURGE_JOURS : jours de journal gardes a chaque demarrage (0 = pas de purge).
#   Les lignes sans date lisible sont effacees.
PORT = 8083
DOSSIER_RECEPTION = D:/UPL
TAILLE_MAX_MO = 2048
EXTENSIONS = zip,rar,7z
NB_MAX_FICHIERS = 20
VITESSE_MAX_MO_S = 0
PURGE_JOURS = 30
"""

# Premiers octets d'une vraie archive, par extension.
MAGIES = {
    "zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "rar": [b"Rar!"],
    "7z": [b"7z\xbc\xaf\x27\x1c"],
}

TAILLE_MORCEAU = 1024 * 1024  # ecriture par morceaux de 1 Mo


def lire_config():
    """Lit dumpbox-config.conf. Si absent, cree le modele et retourne None."""
    if not os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                f.write(MODELE_CONFIG)
            print("Config creee : %s — verifie-la puis relance." % CONFIG_FILE)
        except OSError as exc:
            print("Impossible de creer %s : %s" % (CONFIG_FILE, exc))
        return None
    cfg = dict(DEFAUTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "=" not in ligne:
                    continue
                nom, valeur = ligne.split("=", 1)
                cfg[nom.strip()] = valeur.strip()
    except OSError as exc:
        print("Config illisible (%s), valeurs par defaut." % exc)
        return dict(DEFAUTS)
    try:
        cfg["PORT"] = int(cfg["PORT"])
    except (ValueError, TypeError):
        cfg["PORT"] = DEFAUTS["PORT"]
    try:
        cfg["TAILLE_MAX_MO"] = int(cfg["TAILLE_MAX_MO"])
    except (ValueError, TypeError):
        cfg["TAILLE_MAX_MO"] = DEFAUTS["TAILLE_MAX_MO"]
    try:
        cfg["NB_MAX_FICHIERS"] = int(cfg["NB_MAX_FICHIERS"])
        if cfg["NB_MAX_FICHIERS"] < 1:
            raise ValueError
    except (ValueError, TypeError):
        cfg["NB_MAX_FICHIERS"] = DEFAUTS["NB_MAX_FICHIERS"]
    try:
        cfg["VITESSE_MAX_MO_S"] = int(cfg["VITESSE_MAX_MO_S"])
        if cfg["VITESSE_MAX_MO_S"] < 0:
            raise ValueError
    except (ValueError, TypeError):
        cfg["VITESSE_MAX_MO_S"] = DEFAUTS["VITESSE_MAX_MO_S"]
    try:
        cfg["PURGE_JOURS"] = int(cfg["PURGE_JOURS"])
        if cfg["PURGE_JOURS"] < 0:
            raise ValueError
    except (ValueError, TypeError):
        cfg["PURGE_JOURS"] = DEFAUTS["PURGE_JOURS"]
    cfg["EXTENSIONS"] = [e.strip().lower().lstrip(".")
                         for e in str(cfg["EXTENSIONS"]).split(",") if e.strip()]
    if not cfg["EXTENSIONS"]:
        cfg["EXTENSIONS"] = ["zip", "rar", "7z"]
    return cfg


def journal(msg):
    """Une ligne dans log/dumpbox.log + dans la fenetre du .bat."""
    ligne = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(ligne, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except OSError:
        pass


def progression(recus, total, nom, ecoule=0):
    """Une seule ligne qui se met a jour dans la fenetre du .bat.

    Affiche aussi la vitesse moyenne depuis le debut de l'envoi,
    en Mo/s decimaux (meme unite que VITESSE_MAX_MO_S) : on voit
    tout de suite si la limite mord.
    """
    pct = (100.0 * recus / total) if total else 0.0
    vitesse = (recus / ecoule / 1000000) if ecoule > 0 else 0.0
    sys.stdout.write("\rTransfert : %d / %d Mo (%.0f %%) — %.1f Mo/s — %s"
                     % (recus // (1024 * 1024), total // (1024 * 1024), pct,
                        vitesse, nom))
    sys.stdout.flush()


def nom_sain(nom):
    """Enleve tout chemin et tout caractere dangereux d'un nom de fichier."""
    nom = os.path.basename(nom.replace("\\", "/"))
    nom = "".join(c for c in nom if c.isalnum() or c in "._- ()")
    nom = nom.strip(" .")
    # Tronque la base, jamais l'extension : sinon un long nom perd
    # son .rar/.zip et serait refuse a tort (vu dans le log : des
    # noms de 120 caracteres coupes en ".p" ou sans extension).
    base, ext = os.path.splitext(nom)
    base = base[:max(0, 120 - len(ext))]
    return (base + ext).strip(" .")


def chemin_unique(dossier, nom):
    """Chemin libre : ajoute " (2)", " (3)" si le nom existe deja."""
    base, ext = os.path.splitext(nom)
    chemin = os.path.join(dossier, nom)
    n = 2
    while os.path.exists(chemin):
        chemin = os.path.join(dossier, "%s (%d)%s" % (base, n, ext))
        n += 1
    return chemin


PAGE_CSS = (
    "body{background:#0d0d0d;color:#e8ecf4;font-family:Arial,sans-serif;"
    "margin:0} .wrap{max-width:900px;margin:0 auto;padding:24px}"
    "h1{letter-spacing:3px} .boite{border:2px dashed #7dd3fc;border-radius:12px;"
    "padding:28px;text-align:center} input{margin:16px 0} button{background:#7dd3fc;"
    "border:0;border-radius:8px;padding:12px 28px;font-size:16px;cursor:pointer}"
    ".ok{color:#7dd3fc} .err{color:#f87171} .note{color:#9aa3b2;font-size:13px}"
    "a{color:#7dd3fc} button.x{padding:2px 7px;font-size:12px}"
    " #liste{text-align:left} #liste div{display:flex;gap:6px}"
    " #liste button{flex-shrink:0;align-self:flex-start}")


def page_formulaire(cfg):
    """Page d'envoi. Sans JavaScript, le formulaire envoie le lot
    d'un coup (page de recap a la fin). Avec JavaScript, les fichiers
    partent un par un, avec liste modifiable, total, progression et
    annulation."""
    exts = ", ".join("." + e for e in cfg["EXTENSIONS"])
    accept = ",".join("." + e for e in cfg["EXTENSIONS"])
    page = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>DUMP BOX</title><style>__CSS__</style></head><body><div class="wrap">
<h1>DUMP BOX</h1>
<p>Deposez un ou plusieurs fichiers archives, ils seront remis au proprietaire.<br>
<span class="note">Drop one or more archive files, they will be delivered to the owner.</span></p>
<div class="boite"><form id="f" method="post" enctype="multipart/form-data">
<input type="file" id="choix" name="fichier" accept="__ACCEPT__" multiple required><br>
<button type="submit" id="envoyer">Envoyer / Send</button>
<button type="button" id="effacer">Tout effacer / Clear all</button>
<button type="button" id="annuler" disabled>Annuler / Cancel</button>
</form>
<div id="msg" class="err"></div>
<div id="liste"></div>
<div id="total"></div>
<progress id="barre" value="0" max="100" style="width:100%;display:none"></progress>
<div id="progres"></div>
<div id="recap"></div></div>
<p class="note">Archives seulement : __EXTS__ — Taille max : __TAILLE__ Mo par fichier, __MAXF__ fichiers max par envoi.<br>
Aucune liste, aucun telechargement : les depots ne sont visibles que par le proprietaire.</p>
</div>
<script>
var MAXF = __MAXF__;
var TAILLE_MAX_MO = __TAILLE__;
var TAILLE_MAX_OCTETS = __TAILLE__ * 1024 * 1024;
var input = document.getElementById('choix');
var fichiers = [];
var xhrActuel = null;
var annule = false;

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function tailleHumaine(o) {
  if (o >= 1073741824) return (o / 1073741824).toFixed(2) + ' Go';
  if (o >= 1048576) return (o / 1048576).toFixed(1) + ' Mo';
  return o + ' octets';
}
function majListe() {
  try {
    var dt = new DataTransfer();
    fichiers.forEach(function (f) { dt.items.add(f); });
    input.files = dt.files;
  } catch (e) {}
  var html = '';
  fichiers.forEach(function (f, i) {
    html += '<div><button type="button" class="x" onclick="enlever(' + i + ')">x</button><span>'
      + esc(f.name) + ' (' + tailleHumaine(f.size) + ')</span></div>';
  });
  document.getElementById('liste').innerHTML = html;
  var total = 0;
  fichiers.forEach(function (f) { total += f.size; });
  document.getElementById('total').textContent =
    fichiers.length + ' fichier(s) — Total : ' + tailleHumaine(total);
}
function enlever(i) { fichiers.splice(i, 1); majListe(); }
document.getElementById('effacer').onclick = function () {
  fichiers = [];
  document.getElementById('recap').innerHTML = '';
  document.getElementById('progres').textContent = '';
  majListe();
};
input.addEventListener('change', function () {
  var choisis = Array.prototype.slice.call(input.files);
  var gardes = [];
  var tropGros = [];
  choisis.forEach(function (f) {
    if (f.size > TAILLE_MAX_OCTETS) { tropGros.push(f.name); }
    else { gardes.push(f); }
  });
  var avert = '';
  if (tropGros.length) {
    avert += tropGros.length + ' fichier(s) trop gros exclus (max '
      + TAILLE_MAX_MO + ' Mo). ';
  }
  if (gardes.length > MAXF) {
    avert += 'Maximum ' + MAXF + ' fichiers par envoi : seuls les '
      + MAXF + ' premiers sont gardes.';
    gardes = gardes.slice(0, MAXF);
  }
  document.getElementById('msg').textContent = avert;
  fichiers = gardes;
  majListe();
});
document.getElementById('f').addEventListener('submit', function (ev) {
  ev.preventDefault();
  if (!fichiers.length) {
    document.getElementById('msg').textContent = 'Choisis au moins un fichier.';
    return;
  }
  annule = false;
  document.getElementById('envoyer').disabled = true;
  document.getElementById('effacer').disabled = true;
  input.disabled = true;
  document.getElementById('annuler').disabled = false;
  document.getElementById('barre').style.display = 'block';
  document.getElementById('recap').innerHTML = '';
  var termines = [];
  var nonTermines = [];
  var totalOctets = 0;
  fichiers.forEach(function (f) { totalOctets += f.size; });
  var envoyesTotal = 0;
  var debutEnvoi = 0;
  var i = 0;
  function afficherRecap() {
    document.getElementById('envoyer').disabled = false;
    document.getElementById('effacer').disabled = false;
    input.disabled = false;
    document.getElementById('annuler').disabled = true;
    xhrActuel = null;
    var html = '';
    if (termines.length) {
      html += '<p class="ok">Fichiers recus et verifies : ' + termines.length + '</p><ul>';
      termines.forEach(function (t) {
        html += '<li>' + esc(t.nom) + ' (' + tailleHumaine(t.taille) + ')</li>';
      });
      html += '</ul>';
    }
    if (nonTermines.length) {
      html += '<p class="err">Non termines : ' + nonTermines.length + '</p><ul>';
      nonTermines.forEach(function (t) {
        html += '<li>' + esc(t.nom) + ' : ' + esc(t.raison) + '</li>';
      });
      html += '</ul>';
    }
    if (!termines.length && !nonTermines.length) {
      html += '<p class="err">Aucun fichier recu.</p>';
    }
    document.getElementById('recap').innerHTML = html;
    document.getElementById('progres').textContent =
      annule ? 'Envoi annule.' : 'Envoi termine.';
  }
  function suivant() {
    if (annule) { afficherRecap(); return; }
    if (i >= fichiers.length) { afficherRecap(); return; }
    var f = fichiers[i];
    var numero = i + 1;
    var fd = new FormData();
    fd.append('fichier', f);
    var xhr = new XMLHttpRequest();
    xhrActuel = xhr;
    xhr.open('POST', '/', true);
    xhr.setRequestHeader('X-Dumpbox-Json', '1');
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var pctFichier = Math.round(100 * e.loaded / e.total);
        var totalEnvoye = envoyesTotal + e.loaded;
        var pctTotal = totalOctets ? Math.round(100 * totalEnvoye / totalOctets) : 0;
        var ecoule = (Date.now() - debutEnvoi) / 1000;
        var vitesse = ecoule > 0 ? (e.loaded / ecoule / 1000000) : 0;
        document.getElementById('progres').textContent =
          'Fichier ' + numero + '/' + fichiers.length + ' — ' + f.name
          + ' : ' + tailleHumaine(e.loaded) + ' / ' + tailleHumaine(e.total)
          + ' (' + pctFichier + '%) — ' + vitesse.toFixed(1) + ' Mo/s'
          + ' — Total : ' + pctTotal + '%';
        document.getElementById('barre').value = pctTotal;
      }
    };
    xhr.onload = function () {
      var rep = null;
      try { rep = JSON.parse(xhr.responseText); } catch (e) {}
      if (rep && rep.ok) { termines.push(rep); }
      else { nonTermines.push({ nom: f.name, raison: (rep && rep.raison) || 'erreur' }); }
      envoyesTotal += f.size;
      xhrActuel = null;
      i++;
      suivant();
    };
    xhr.onerror = function () {
      nonTermines.push({ nom: f.name, raison: 'erreur reseau' });
      envoyesTotal += f.size;
      xhrActuel = null;
      i++;
      suivant();
    };
    xhr.onabort = function () {
      nonTermines.push({ nom: f.name, raison: 'annule' });
      for (var j = i + 1; j < fichiers.length; j++) {
        nonTermines.push({ nom: fichiers[j].name, raison: 'non envoye' });
      }
      afficherRecap();
    };
    debutEnvoi = Date.now();
    xhr.send(fd);
  }
  document.getElementById('annuler').onclick = function () {
    annule = true;
    if (xhrActuel) { xhrActuel.abort(); }
    else { afficherRecap(); }
  };
  suivant();
});
</script>
</body></html>"""
    return (page.replace("__CSS__", PAGE_CSS).replace("__ACCEPT__", accept)
                .replace("__EXTS__", exts).replace("__TAILLE__", str(cfg["TAILLE_MAX_MO"]))
                .replace("__MAXF__", str(cfg["NB_MAX_FICHIERS"])))


def page_recap(recus, refuses):
    """Page finale d'un envoi : recus et verifies, puis refuses."""
    morceaux = ["<h1>DUMP BOX</h1>"]
    if recus:
        morceaux.append("<p class=\"ok\">Fichiers recus et verifies : %d</p><ul>" % len(recus))
        for nom, taille in recus:
            morceaux.append("<li>%s (%d octets)</li>" % (html.escape(nom), taille))
        morceaux.append("</ul>")
    else:
        morceaux.append("<p class=\"err\">Aucun fichier recu.</p>")
    if refuses:
        morceaux.append("<p class=\"err\">Fichiers refuses : %d</p><ul>" % len(refuses))
        for nom, raison in refuses:
            morceaux.append("<li>%s : %s</li>" % (html.escape(nom), html.escape(raison)))
        morceaux.append("</ul>")
    morceaux.append("<p><a href=\"/\">← Nouveau depot / New upload</a></p>")
    return ("<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<title>DUMP BOX</title><style>%s</style></head><body><div class=\"wrap\">"
            "%s</div></body></html>" % (PAGE_CSS, "".join(morceaux)))


def page_message(titre, texte, ok):
    classe = "ok" if ok else "err"
    return ("<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<title>DUMP BOX</title><style>%s</style></head><body><div class=\"wrap\">"
            "<h1>DUMP BOX</h1><p class=\"%s\">%s</p>"
            "<p><a href=\"/\">← Nouveau depot / New upload</a></p>"
            "</div></body></html>" % (PAGE_CSS, classe, texte))


def lire_frontiere(en_tete):
    """La frontiere multipart, depuis l'en-tete Content-Type."""
    for partie in en_tete.split(";"):
        partie = partie.strip()
        if partie.lower().startswith("boundary="):
            frontiere = partie[len("boundary="):].strip().strip('"')
            if 0 < len(frontiere) <= 70:
                return frontiere.encode("utf-8", "replace")
    return None


class LimiteVitesse:
    """Plafond global partage entre tous les envois (0 = illimite).

    Seau a jetons : les jetons se rechargent a `limite` octets/s,
    plafonnes a un morceau. Chaque flux consomme les jetons de ses
    morceaux lus ; s'il n'y en a pas assez, il dort hors verrou
    jusqu'a ce qu'il y en ait. Le plafond ne s'accumule pas pendant
    les periodes calmes : apres une longue inactivite, un nouvel
    envoi est limite des le premier morceau. Le total de tous les
    flux ne depasse jamais la limite, quel que soit leur nombre.
    """

    def __init__(self, limite_octets_s):
        self.limite = limite_octets_s
        self.verrou = threading.Lock()
        self.jetons = 0.0
        self.dernier = time.monotonic()

    def attendre(self, n):
        if self.limite <= 0:
            return
        while True:
            with self.verrou:
                maintenant = time.monotonic()
                self.jetons = min(
                    TAILLE_MORCEAU,
                    self.jetons + (maintenant - self.dernier) * self.limite)
                self.dernier = maintenant
                if self.jetons >= n:
                    self.jetons -= n
                    return
                manque = n - self.jetons
            time.sleep(manque / self.limite)


class LecteurCorps:
    """Lecteur avec tampon, pour parser le multipart en continu.

    Compte tous les octets tires du corps (lignes comprises) : sans
    ca, le calcul du reste serait faux et la lecture attendrait des
    octets deja lus.
    """

    def __init__(self, rfile, total, limiteur=None):
        self.rfile = rfile
        self.buf = b""
        self.restant = total
        self.lus = 0
        # Limiteur global partage (None = illimite).
        self.limiteur = limiteur
        # Debut de l'envoi : sert a la vitesse moyenne affichee.
        self.debut = time.monotonic()

    def _remplir(self):
        if self.restant <= 0:
            return False
        morceau = self.rfile.read(min(TAILLE_MORCEAU, self.restant))
        if not morceau:
            return False
        self.buf += morceau
        self.restant -= len(morceau)
        self.lus += len(morceau)
        if self.limiteur is not None:
            self.limiteur.attendre(len(morceau))
        return True

    def ligne(self):
        """Prochaine ligne (sans \r\n), ou None si le corps est fini."""
        while b"\n" not in self.buf:
            if len(self.buf) > 65536:
                return None  # ligne demesuree : envoi invalide
            if not self._remplir():
                return None
        ligne, self.buf = self.buf.split(b"\n", 1)
        return ligne.rstrip(b"\r")

    def jusqua(self, delim, ecrire, limite_fichier, progression_cb):
        """Ecrit dans ecrire() jusqu'au delimiteur.

        Retourne (taille, depasse, trouve). Si ecrire est None, le
        contenu est juste jete (pour sauter une partie refusee).
        Apres le delimiteur, les octets suivants restent dans buf.
        """
        taille = 0
        depasse = False
        while True:
            idx = self.buf.find(delim)
            if idx >= 0:
                bloc = self.buf[:idx]
                if ecrire is not None and not depasse:
                    ecrire(bloc)
                    taille += len(bloc)
                    if taille > limite_fichier:
                        depasse = True
                self.buf = self.buf[idx + len(delim):]
                return taille, depasse, True
            if len(self.buf) > len(delim):
                bloc = self.buf[:-len(delim)]
                if ecrire is not None and not depasse:
                    ecrire(bloc)
                    taille += len(bloc)
                    if taille > limite_fichier:
                        depasse = True
                self.buf = self.buf[-len(delim):]
            progression_cb(self.lus, time.monotonic() - self.debut)
            if not self._remplir():
                return taille, depasse, False


def recevoir_fichiers(handler, frontiere, longueur, limite, dossier, limiteur=None):
    """Traite toutes les parties fichier d'un envoi.

    Retourne (recus, refuses) : recus = [(nom_final, taille)],
    refuses = [(nom, raison)]. Un fichier deja recu reste recu meme
    si une partie suivante echoue. Les fichiers temporaires des
    echecs sont effaces.
    """
    cfg = handler.cfg
    lecteur = LecteurCorps(handler.rfile, longueur, limiteur)
    delim = b"\r\n--" + frontiere
    recus = []
    refuses = []
    compte = 0
    ip = handler.address_string()

    def note_refus(nom_, raison):
        # Journal immediat : si la connexion coupe apres, le fichier
        # reste trace (la v1.2 ne journalisait qu'a la fin).
        refuses.append((nom_, raison))
        journal("%s REFUSE %s (%s)" % (ip, nom_, raison))

    def note_depot(nom_, taille):
        recus.append((nom_, taille))
        journal("%s DEPOT %s (%d octets)" % (ip, nom_, taille))

    if lecteur.ligne() != b"--" + frontiere:
        return recus, refuses
    while True:
        # En-tetes de la partie.
        nom_brut = None
        for _ in range(20):
            ligne = lecteur.ligne()
            if ligne is None:
                return recus, refuses  # corps tronque : on garde l'acquis
            if ligne == b"":
                break
            texte = ligne.decode("utf-8", "replace")
            if 'name="fichier"' in texte and 'filename="' in texte:
                debut = texte.find('filename="') + len('filename="')
                fin = texte.find('"', debut)
                nom_brut = texte[debut:fin] if fin >= 0 else ""
        nom = nom_sain(nom_brut) if nom_brut else ""
        if nom_brut is not None:
            compte += 1
        if nom_brut is not None and compte > cfg["NB_MAX_FICHIERS"]:
            # Au-dela de la limite : le fichier est jete, pas recu.
            note_refus(nom or nom_brut,
                       "limite de %d fichiers par envoi" % cfg["NB_MAX_FICHIERS"])
            lecteur.jusqua(delim, None, 0,
                           lambda lus, ecoule: progression(lus, longueur, nom, ecoule))
        elif not nom:
            if nom_brut:
                note_refus(nom_brut, "nom invalide")
            lecteur.jusqua(delim, None, 0, lambda lus, ecoule: None)
        else:
            ext = os.path.splitext(nom)[1].lower().lstrip(".")
            if ext not in cfg["EXTENSIONS"]:
                note_refus(nom, "extension interdite")
                lecteur.jusqua(delim, None, 0,
                               lambda lus, ecoule: progression(lus, longueur, nom, ecoule))
            else:
                tmp = os.path.join(dossier, ".tmp-%d-%s" % (os.getpid(), nom))
                try:
                    with open(tmp, "wb") as f:
                        taille, depasse, trouve = lecteur.jusqua(
                            delim, f.write, limite,
                            lambda lus, ecoule: progression(lus, longueur, nom, ecoule))
                except ConnectionError:
                    # Fenetre fermee ou connexion coupee : le partiel
                    # est efface, le fichier est note coupe, et on
                    # rend l'acquis (deja journalise fichier par fichier).
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    note_refus(nom, "transfert coupe")
                    return recus, refuses
                except OSError:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    note_refus(nom, "ecriture")
                    continue
                if not trouve:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    note_refus(nom, "transfert coupe")
                elif depasse:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    note_refus(nom, "trop gros")
                else:
                    with open(tmp, "rb") as f:
                        debut = f.read(8)
                    if not any(debut.startswith(m) for m in MAGIES.get(ext, [])):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        note_refus(nom, "pas une vraie archive")
                    else:
                        final = chemin_unique(dossier, nom)
                        try:
                            os.replace(tmp, final)
                        except OSError:
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                            note_refus(nom, "ecriture")
                        else:
                            note_depot(os.path.basename(final), taille)
        # Apres le delimiteur : "--" = fermeture, sinon une autre partie.
        while len(lecteur.buf) < 2 and lecteur._remplir():
            pass
        if lecteur.buf[:2] == b"--":
            break
        lecteur.ligne()  # consomme le "\r\n" de separation
    # Jete un eventuel reste (fin de corps) pour garder la connexion propre.
    while lecteur.restant > 0:
        morceau = lecteur.rfile.read(min(TAILLE_MORCEAU, lecteur.restant))
        if not morceau:
            break
        lecteur.restant -= len(morceau)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return recus, refuses


class ServeurWeb(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Un visiteur qui coupe sa connexion est normal : on l'ignore.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        journal("Erreur HTTP : %r" % (sys.exc_info()[1],))


class HandlerDepot(BaseHTTPRequestHandler):
    cfg = None
    limiteur = None

    def log_message(self, *args):
        pass  # journal propre dans log/dumpbox.log, pas dans la console

    def _repondre(self, code, corps_html):
        donnees = corps_html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(donnees)))
        self.end_headers()
        try:
            self.wfile.write(donnees)
        except ConnectionError:
            pass

    def do_GET(self):
        # Seule la page du formulaire existe. Tout le reste : 404,
        # y compris les fichiers recus (jamais servis).
        if urlsplit(self.path).path in ("", "/"):
            self._repondre(200, page_formulaire(self.cfg))
        else:
            self._repondre(404, page_message("DUMP BOX", "Introuvable.", False))

    def _repondre_json(self, donnees):
        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        try:
            self.wfile.write(corps)
        except ConnectionError:
            pass

    def do_POST(self):
        cfg = self.cfg
        if urlsplit(self.path).path not in ("", "/"):
            self._repondre(404, page_message("DUMP BOX", "Introuvable.", False))
            return
        try:
            longueur = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            longueur = 0
        limite = cfg["TAILLE_MAX_MO"] * 1024 * 1024
        if longueur <= 0:
            self._repondre(400, page_message("DUMP BOX", "Aucun fichier recu.", False))
            return
        # Pas de plafond sur le corps total : avec plusieurs fichiers,
        # le total depasse legitimement la limite par fichier. Chaque
        # fichier est plafonne pendant son ecriture.
        frontiere = lire_frontiere(self.headers.get("Content-Type", ""))
        if frontiere is None:
            self._repondre(400, page_message("DUMP BOX", "Envoi invalide.", False))
            return
        # La page JavaScript envoie un fichier par requete et attend
        # une reponse JSON ; le formulaire classique attend la recap HTML.
        mode_json = self.headers.get("X-Dumpbox-Json", "") == "1"
        try:
            recus, refuses = recevoir_fichiers(
                self, frontiere, longueur, limite, cfg["DOSSIER_RECEPTION"], self.limiteur)
        except ConnectionError:
            # Les fichiers deja recus sont journalises (un par un dans
            # recevoir_fichiers) ; il ne manque que la ligne de fin.
            journal("%s ENVOI interrompu (connexion coupee)"
                    % self.address_string())
            return
        journal("%s ENVOI : %d recus, %d refuses"
                % (self.address_string(), len(recus), len(refuses)))
        if mode_json:
            if recus:
                nom, taille = recus[0]
                self._repondre_json({"ok": True, "nom": nom, "taille": taille})
            elif refuses:
                nom, raison = refuses[0]
                self._repondre_json({"ok": False, "nom": nom, "raison": raison})
            else:
                self._repondre_json({"ok": False, "nom": "", "raison": "aucun fichier"})
        else:
            self._repondre(200, page_recap(recus, refuses))


_MOIS_PURGE = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _date_ligne_journal(ligne):
    """Date (annee, mois, jour) d'une ligne de journal, ou None si illisible.
    Comprend les dates ISO en debut de ligne et les dates CLF [JJ/Mmm/AAAA]."""
    m = re.match(r"\[?(\d{4})-(\d{2})-(\d{2})\b", ligne)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\[(\d{2})/([A-Za-z]{3})/(\d{4})", ligne)
    if m and m.group(2) in _MOIS_PURGE:
        return (int(m.group(3)), _MOIS_PURGE[m.group(2)], int(m.group(1)))
    return None


def purger_journal(chemin, jours):
    """Garde seulement les lignes des `jours` derniers jours dans le journal.
    0 (ou moins) = pas de purge. Une ligne sans date lisible est effacee :
    on ne peut pas prouver qu'elle a moins de `jours` jours.
    Retourne (gardees, effacees) ou None si rien a faire."""
    if jours <= 0:
        return None
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            lignes = f.readlines()
    except OSError:
        return None
    limite = time.localtime(time.time() - jours * 86400)[:3]
    gardees = []
    for ligne in lignes:
        d = _date_ligne_journal(ligne)
        if d is not None and d >= limite:
            gardees.append(ligne)
    if len(gardees) == len(lignes):
        return (len(gardees), 0)
    try:
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(gardees)
        os.replace(tmp, chemin)
    except OSError:
        return None
    return (len(gardees), len(lignes) - len(gardees))


def main():
    cfg = lire_config()
    if cfg is None:
        return
    res_purge = purger_journal(LOG_FILE, cfg["PURGE_JOURS"])
    if res_purge:
        journal("Purge journal : %d ligne(s) gardee(s), %d effacee(s) (%d derniers jours)"
                % (res_purge[0], res_purge[1], cfg["PURGE_JOURS"]))
    try:
        os.makedirs(cfg["DOSSIER_RECEPTION"], exist_ok=True)
    except OSError as exc:
        print("Impossible de creer %s : %s" % (cfg["DOSSIER_RECEPTION"], exc))
        return
    # Menage au demarrage : un .tmp-* restant vient d'un envoi coupe
    # (fenetre fermee, reseau coupe) — il ne sert plus a rien.
    try:
        restes = [n for n in os.listdir(cfg["DOSSIER_RECEPTION"])
                  if n.startswith(".tmp-")]
    except OSError:
        restes = []
    for nom_tmp in restes:
        try:
            os.remove(os.path.join(cfg["DOSSIER_RECEPTION"], nom_tmp))
        except OSError:
            pass
    if restes:
        journal("Menage demarrage : %d fichier(s) temporaire(s) supprime(s)"
                % len(restes))
    HandlerDepot.cfg = cfg
    # Un seul limiteur pour tout le serveur : le plafond est partage
    # entre tous les envois, quel que soit leur nombre. En Mo/s
    # DECIMAUX (par 1000, pas par 1024) ; 0 = illimite.
    HandlerDepot.limiteur = LimiteVitesse(cfg["VITESSE_MAX_MO_S"] * 1000 * 1000)
    try:
        serveur = ServeurWeb(("0.0.0.0", cfg["PORT"]), HandlerDepot)
    except OSError as exc:
        print("ERREUR : impossible d'ouvrir le port %d (%s)" % (cfg["PORT"], exc))
        return
    journal("DUMP BOX v%s actif sur le port %d" % (VERSION, cfg["PORT"]))
    vitesse_txt = ("illimite" if cfg["VITESSE_MAX_MO_S"] <= 0
                   else "%d Mo/s" % cfg["VITESSE_MAX_MO_S"])
    journal("Dossier de reception : %s (max %d Mo, %d fichiers max, vitesse globale %s, archives : %s)"
            % (cfg["DOSSIER_RECEPTION"], cfg["TAILLE_MAX_MO"],
               cfg["NB_MAX_FICHIERS"], vitesse_txt,
               ", ".join("." + e for e in cfg["EXTENSIONS"])))
    journal("Journal : log/dumpbox.log — Ctrl+C pour arreter.")
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
