# -*- coding: utf-8 -*-
"""
Mailbot FTP - repond automatiquement aux demandes de fichiers par courriel.

Le client envoie un courriel a la boite dediee :
  - sujet "LISTE"          -> recoit la liste complete des fichiers, chacun
                              avec son lien FTP cliquable
  - sujet = nom (ou bout de nom) d'un fichier
                           -> recoit le lien FTP direct du fichier
  - sujet "AIDE"           -> recoit les instructions

Prerequis : Python 3 installe (rien d'autre, tout est dans la librairie standard).
Concu pour FileZilla Server avec utilisateur anonymous.
"""

# ==================== VERSION ====================
# Numero de version du script (MAJEUR.MINEUR) — a afficher dans la
# console au demarrage, pour savoir quelle copie roule ou :
#   - changement MAJEUR (nouvelle fonction, changement de comportement)
#     -> MAJEUR + 1, MINEUR remis a 0  (ex. 2.0 -> 3.0)
#   - petit changement (correctif, texte, reglage)
#     -> MINEUR + 1  (ex. 2.0 -> 2.1)
# v2.2 : purge du journal au demarrage (PURGE_JOURS, defaut 30 jours).
# v2.4 : journal reseau (log/reseau.log) + anti-boucle pairs + sujets
#   nettoyes (plus de saut de ligne dans les en-tetes) + avertissement
#   si un pair porte le meme NOM_SERVEUR que ce serveur.
# v2.3 : entete reseau unifiee (LISTE-RESEAU et RELAIS-LISTE) + mention
#   « partie 1 de 1 » sur les listes en une seule partie.
VERSION = "2.4"

import imaplib
import smtplib
import os
import sys
import time
import csv
import html
import re
import base64
import secrets
import threading
import urllib.request
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import quote, urlsplit, parse_qs

# ================= CONFIGURATION - A REMPLIR =================
COURRIEL      = "freq@exemple.com"       # boite courriel dediee au robot
MOT_DE_PASSE  = "REMETS-TON-MOT-DE-PASSE-ICI"
IMAP_SERVEUR  = "127.0.0.1"                # ton serveur MDaemon local
IMAP_PORT     = 143                        # 143 = IMAP simple | 993 = IMAP SSL
IMAP_SSL      = False                      # False pour le port 143, True pour le port 993
SMTP_SERVEUR  = "127.0.0.1"
SMTP_PORT     = 25                         # 25 = SMTP simple | 587 = soumission STARTTLS
SMTP_STARTTLS = False                      # False pour le port 25, True pour le port 587
FTP_HOTE      = "127.0.0.1"                # adresse PUBLIQUE que les clients utiliseront (domaine ou IP externe)
FTP_RACINE    = r"D:\Fichiers"                # dossier racine servi par ton FileZilla Server
HTTP_PORT     = 8080                       # port du mini-serveur de telechargement (liens http://)
HTTP_MOT_DE_PASSE = ""                     # mot de passe d'acces au 8080 ; VIDE = ouvert a tous (comme maintenant)
                                           # (pas de nom d'utilisateur a gerer : seul le mot de passe compte)
NOM_SERVEUR   = "MonServeur"                # nom de TON serveur dans le reseau FREQ
RESEAU_MOT_DE_PASSE = ""                   # mot de passe du RESEAU (optionnel) : si defini, un serveur
                                           # qui s'annonce (ANNOUNCE) doit fournir la meme cle (ligne CLE:)
MODE_JETON    = False                      # True = acces par LIENS A JETON : chaque courriel contient des
                                           # liens avec un jeton unique, valide DUREE_JETON minutes, pour
                                           # CE demandeur (aucun mot de passe a taper). Le serveur devient
                                           # MUET : aucune navigation dans les dossiers, tout lien sans
                                           # jeton valide recoit 404. HTTP_MOT_DE_PASSE reste accepte
                                           # comme cle maitre (mais n'est plus imprime dans les courriels)
DUREE_JETON   = 60                         # duree de vie d'un jeton, en minutes
LISTE_TAILLE_MAX_MO = 6                    # taille max visee par courriel de LISTE, en Mo — taille FINALE
                                           # du courriel (texte + version HTML + encodage). Au-dela, la
                                           # liste part en plusieurs courriels numerotes (partie 1/3...).
                                           # Borne par le code entre 2 et 10 Mo : plus petit = trop de
                                           # courriels, plus gros = ouverture lente (et limite des
                                           # boites autour de 20-25 Mo).
DELAI_VERIF   = 60                         # secondes entre chaque verification de la boite
NETTOYAGE_BOITE = True                     # True = le robot supprime tout seul les courriels DEJA
                                           # TRAITES (lus) plus vieux que GARDER_JOURS jours, pour
                                           # que la boite ne s'accumule pas. Les courriels pas
                                           # encore traites ne sont JAMAIS touches.
GARDER_JOURS  = 7                          # jours a garder les courriels traites avant de les
                                           # supprimer (0 = supprimer tous les traites a chaque menage)
PURGE_JOURS   = 30                         # jours de journal gardes a chaque demarrage (0 = pas de purge) :
                                           # log/bot.log ne garde que les lignes des 30 derniers jours.
                                           # Une ligne sans date lisible est effacee.
EXPEDITEURS_OK = []                        # [] = tout le monde ; sinon ["ami@exemple.com", ...]
# ==============================================================

# ---------- Lecture de config.conf ----------
# Si un fichier nomme config.conf existe A COTE du script, ses valeurs
# REMPLACENT celles ci-dessus. C'est la que tu mets TES reglages :
# config.conf n'est JAMAIS ecrase quand mailbot-ftp.py est mis a jour,
# donc plus jamais de re-saisie apres une mise a jour.
# Format, une ligne par reglage :  NOM = valeur   (les # sont des commentaires)
CONFIG_SOURCE = None

def _charger_config():
    global CONFIG_SOURCE
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.conf")
    if not os.path.exists(chemin):
        return
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, valeur = ligne.split("=", 1)
            cle = cle.strip().upper()
            valeur = valeur.strip()
            if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
                valeur = valeur[1:-1]
            if cle not in globals():
                continue   # reglage inconnu : ignore
            actuel = globals()[cle]
            if isinstance(actuel, bool):
                globals()[cle] = valeur.lower() in ("true", "vrai", "oui", "yes", "1")
            elif isinstance(actuel, int):
                try:
                    globals()[cle] = int(valeur)
                except ValueError:
                    pass
            elif isinstance(actuel, list):
                globals()[cle] = [m.strip() for m in valeur.split(",") if m.strip()]
            else:
                globals()[cle] = valeur
    CONFIG_SOURCE = chemin

_charger_config()


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def log_reseau(msg):
    """Journal reseau (v2.4) : les evenements de federation, en plus de
    la console, dans log/reseau.log (date en tete, donc purgeable
    comme bot.log). Ne sert qu'au reseau, jamais aux acces HTTP."""
    ligne = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg.strip())
    try:
        with open(RESEAU_LOG, "a", encoding="utf-8") as f:
            f.write(ligne)
    except OSError:
        pass


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


def decodage(valeur):
    """Decode un en-tete courriel (=?utf-8?...?=) en texte normal."""
    if not valeur:
        return ""
    morceaux = []
    for data, enc in decode_header(valeur):
        if isinstance(data, bytes):
            morceaux.append(data.decode(enc or "utf-8", errors="replace"))
        else:
            morceaux.append(data)
    return "".join(morceaux).strip()


def corps_texte(msg):
    """Extrait le texte brut du corps du courriel."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                brut = part.get_payload(decode=True)
                if brut:
                    return brut.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    brut = msg.get_payload(decode=True)
    if brut:
        return brut.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def catalogue():
    """Liste tous les fichiers sous FTP_RACINE (chemins relatifs)."""
    fichiers = []
    for racine, _, noms in os.walk(FTP_RACINE):
        for n in noms:
            fichiers.append(os.path.relpath(os.path.join(racine, n), FTP_RACINE))
    return sorted(fichiers, key=str.lower)


def lien_ftp(rel):
    """Construit le lien FTP d'un fichier (pour un client FTP, ex. FileZilla Client)."""
    return "ftp://" + FTP_HOTE + "/" + quote(rel.replace("\\", "/"))


def lien_http(rel):
    """Lien de telechargement direct pour navigateur.
    Les navigateurs modernes (Chrome, Edge, Firefox...) ne supportent plus le FTP :
    c'est ce lien-la que le client clique dans son courriel.
    En MODE_JETON, le jeton de la demande en cours est DANS le lien
    (http://hote:port/<jeton>/fichier) : aucun mot de passe a taper,
    et le serveur muet refuse tout lien sans jeton valide."""
    base = "http://" + FTP_HOTE + ":" + str(HTTP_PORT) + "/"
    if MODE_JETON and _jeton_demande:
        base += _jeton_demande + "/"
    return base + quote(rel.replace("\\", "/"))


def demarrer_http():
    """Mini-serveur web qui sert FTP_RACINE en telechargement direct."""
    charger_stats()
    serveur = ServeurHTTP(("0.0.0.0", HTTP_PORT), HandlerTelechargement)
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    log("Serveur de telechargement HTTP actif sur le port %d (racine : %s)" % (HTTP_PORT, FTP_RACINE))


# ================= STATISTIQUES DE TELECHARGEMENT =================
DOSSIER_SCRIPT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(DOSSIER_SCRIPT, "log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass
STATS_CSV = os.path.join(LOG_DIR, "stats-telechargements.csv")
STATS_TXT = os.path.join(DOSSIER_SCRIPT, "stats-resume.txt")
BOT_LOG = os.path.join(LOG_DIR, "bot.log")   # journal d'acces du serveur (les lignes GET, sondes de robots...)
RESEAU_LOG = os.path.join(LOG_DIR, "reseau.log")  # journal reseau (ANNOUNCE, RESEAU, LISTE-RESEAU, RELAIS...)
RESEAU_TXT = os.path.join(DOSSIER_SCRIPT, "reseau.txt")  # annuaire des autres serveurs FREQ
_verrou_log = threading.Lock()
_verrou_stats = threading.Lock()
_compteur_fichiers = Counter()
_compteur_ips = Counter()
_total_dl = 0
_derniers_dl = []   # (date, ip, fichier), les 20 plus recents


def charger_stats():
    """Relit le CSV au demarrage pour garder les compteurs d'une session a l'autre."""
    global _total_dl
    if not os.path.exists(STATS_CSV):
        return
    with open(STATS_CSV, newline="", encoding="utf-8") as f:
        for ligne in csv.reader(f, delimiter=";"):
            if len(ligne) != 3 or ligne[0] == "Date":
                continue
            date, ip, fichier = ligne
            _total_dl += 1
            _compteur_fichiers[fichier] += 1
            _compteur_ips[ip] += 1
            _derniers_dl.append((date, ip, fichier))
    del _derniers_dl[:-20]
    if _total_dl:
        log("Stats rechargees : %d telechargements deja enregistres" % _total_dl)


def ecrire_resume():
    lignes = ["Statistiques de telechargement",
              "Mise a jour : " + time.strftime("%Y-%m-%d %H:%M:%S"),
              "Total : %d telechargement(s)" % _total_dl, "",
              "Par fichier :"]
    for fichier, n in _compteur_fichiers.most_common():
        lignes.append("  %4d x %s" % (n, fichier))
    lignes += ["", "Par adresse IP :"]
    for ip, n in _compteur_ips.most_common():
        lignes.append("  %4d x %s" % (n, ip))
    lignes += ["", "Derniers telechargements :"]
    for date, ip, fichier in reversed(_derniers_dl):
        lignes.append("  %s  %-15s  %s" % (date, ip, fichier))
    with open(STATS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


def enregistrer_telechargement(ip, fichier):
    global _total_dl
    date = time.strftime("%Y-%m-%d %H:%M:%S")
    with _verrou_stats:
        nouveau = not os.path.exists(STATS_CSV)
        with open(STATS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if nouveau:
                w.writerow(["Date", "IP", "Fichier"])
            w.writerow([date, ip, fichier])
        _total_dl += 1
        _compteur_fichiers[fichier] += 1
        _compteur_ips[ip] += 1
        _derniers_dl.append((date, ip, fichier))
        del _derniers_dl[:-20]
        ecrire_resume()
    log("Telechargement : %s par %s" % (fichier, ip))


# ================= RESEAU FREQ (federation de serveurs) =================
# Chaque serveur garde la liste des autres dans reseau.txt (a cote du script).
# Inscription : un serveur envoie un courriel ANNOUNCE avec son adresse ;
# on verifie qu'un vrai serveur FREQ repond a cette adresse (page
# /freq-info.txt), on l'ajoute, on lui renvoie la liste complete du reseau
# (courriel RESEAU) et on propage aux autres serveurs connus.

def mon_adresse():
    """Adresse publique de ce serveur (hote:port)."""
    if ":" in FTP_HOTE:
        return FTP_HOTE
    return FTP_HOTE + ":" + str(HTTP_PORT)


def charger_reseau():
    pairs = []
    if os.path.exists(RESEAU_TXT):
        with open(RESEAU_TXT, encoding="utf-8") as f:
            for ligne in f:
                morceaux = [m.strip() for m in ligne.split(";")]
                if len(morceaux) >= 3 and morceaux[1] and not morceaux[0].startswith("#"):
                    pairs.append({"nom": morceaux[0], "adresse": morceaux[1],
                                  "courriel": morceaux[2],
                                  "vu": morceaux[3] if len(morceaux) > 3 else ""})
    return pairs


def sauver_reseau(pairs):
    with open(RESEAU_TXT, "w", encoding="utf-8") as f:
        f.write("# Reseau FREQ - autres serveurs connus (rempli automatiquement)\n")
        f.write("# Format : Nom ; adresse:port ; courriel du robot ; derniere vue\n")
        for p in pairs:
            f.write("%s ; %s ; %s ; %s\n" % (p["nom"], p["adresse"], p["courriel"], p["vu"]))


def maj_reseau(nom, adresse, courriel):
    """Ajoute ou met a jour un serveur dans l'annuaire. Retourne True si NOUVEAU."""
    if adresse.lower() == mon_adresse().lower():
        return False
    pairs = charger_reseau()
    for p in pairs:
        if p["adresse"].lower() == adresse.lower():
            p["nom"], p["courriel"], p["vu"] = nom, courriel, time.strftime("%Y-%m-%d")
            sauver_reseau(pairs)
            return False
    if len(pairs) >= 200:
        return False
    pairs.append({"nom": nom, "adresse": adresse, "courriel": courriel,
                  "vu": time.strftime("%Y-%m-%d")})
    sauver_reseau(pairs)
    return True


def ping_freq(adresse):
    """Verifie qu'un serveur FREQ repond a cette adresse (page /freq-info.txt).
    Retourne ses infos {nom, courriel} ou None."""
    try:
        with urllib.request.urlopen("http://" + adresse + "/freq-info.txt", timeout=10) as r:
            texte = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    lignes = [l.strip() for l in texte.splitlines()]
    if not lignes or lignes[0] != "FREQ/1.0":
        return None
    infos = {}
    for l in lignes[1:]:
        if ":" in l:
            k, v = l.split(":", 1)
            infos[k.strip().lower()] = v.strip()
    return infos


def envoyer_liste_reseau(destinataire):
    """Envoie l'annuaire complet (format machine) a un serveur du reseau."""
    lignes = ["FREQ-RESEAU/1.0",
              "%s ; %s ; %s" % (NOM_SERVEUR, mon_adresse(), COURRIEL)]
    for p in charger_reseau():
        lignes.append("%s ; %s ; %s" % (p["nom"], p["adresse"], p["courriel"]))
    envoyer(destinataire, "RESEAU", "\n".join(lignes))


def traiter_announce(expediteur, corps):
    champs = {}
    for ligne in corps.splitlines():
        if ":" in ligne:
            k, v = ligne.split(":", 1)
            champs[k.strip().lower()] = v.strip()
    adresse = champs.get("adresse", "")
    if not adresse:
        # Forme courte acceptee : l'adresse SEULE dans le courriel,
        # sans "Adresse:" devant (ex. une ligne "exemple.com:8081",
        # ou meme ecrite apres ANNOUNCE dans le sujet).
        for ligne in corps.splitlines():
            for morceau in ligne.split():
                candidat = morceau
                for prefixe in ("http://", "https://"):
                    if candidat.lower().startswith(prefixe):
                        candidat = candidat[len(prefixe):]
                candidat = candidat.strip("/")
                if re.fullmatch(r"[A-Za-z0-9.-]+(:[0-9]+)?", candidat) and ("." in candidat or ":" in candidat):
                    adresse = candidat
                    break
            if adresse:
                break
    if not adresse:
        log("  -> ANNOUNCE sans adresse (ni 'Adresse:', ni adresse seule), ignore")
        log_reseau("ANNOUNCE de %s : sans adresse, ignore" % expediteur)
        return
    if RESEAU_MOT_DE_PASSE and champs.get("cle", "") != RESEAU_MOT_DE_PASSE:
        log("  -> ANNOUNCE refuse : cle du reseau absente ou mauvaise")
        log_reseau("ANNOUNCE de %s : refuse (cle absente ou mauvaise)" % expediteur)
        return
    infos = ping_freq(adresse)
    if infos is None:
        log("  -> ANNOUNCE non verifie (aucun serveur FREQ a %s), ignore" % adresse)
        log_reseau("ANNOUNCE de %s (%s) : non verifie, ignore" % (expediteur, adresse))
        return
    nom = infos.get("nom") or champs.get("nom") or adresse
    courriel_pair = infos.get("courriel") or champs.get("courriel") or expediteur
    nouveau = maj_reseau(nom, adresse, courriel_pair)
    log("  -> serveur %s : %s (%s)" % ("NOUVEAU" if nouveau else "mis a jour", nom, adresse))
    log_reseau("ANNOUNCE de %s : serveur %s : %s (%s)"
               % (expediteur, "NOUVEAU" if nouveau else "mis a jour", nom, adresse))
    if nom == NOM_SERVEUR:
        log("  -> ATTENTION : ce pair porte le meme nom que ce serveur (%s) : "
            "change NOM_SERVEUR dans son config pour un nom unique" % nom)
        log_reseau("ATTENTION : pair %s (%s) porte le meme nom que ce serveur" % (nom, adresse))
    try:
        envoyer_liste_reseau(expediteur)   # le nouveau repart avec l'annuaire complet
    except Exception as e:
        log("  -> envoi RESEAU a %s echoue : %r" % (expediteur, e))
        log_reseau("ANNOUNCE de %s : envoi RESEAU echoue : %r" % (expediteur, e))
    if nouveau:   # propager l'arrivee aux autres serveurs connus
        for p in charger_reseau():
            if p["adresse"].lower() != adresse.lower() and p["courriel"]:
                try:
                    envoyer_liste_reseau(p["courriel"])
                except Exception:
                    pass


def traiter_reseau(corps):
    ajoutes = 0
    for ligne in corps.splitlines():
        morceaux = [m.strip() for m in ligne.split(";")]
        if len(morceaux) >= 3 and morceaux[1]:
            if maj_reseau(morceaux[0], morceaux[1], morceaux[2]):
                ajoutes += 1
    log("  -> liste RESEAU fusionnee (%d nouveau(x), %d serveur(s) connus)"
        % (ajoutes, len(charger_reseau())))
    log_reseau("RESEAU fusionne : %d nouveau(x), %d serveur(s) connus"
               % (ajoutes, len(charger_reseau())))


def _corps_resultats(terme, trouves):
    """Formate une liste de resultats de recherche (max 100 affiches)."""
    lignes = []
    for f in trouves[:100]:
        lignes.append(f)
        lignes.append("   " + lien_http(f))
        lignes.append("")
    if len(trouves) > 100:
        lignes.append("... et %d autre(s) resultat(s) - affine ta recherche." % (len(trouves) - 100))
        lignes.append("... and %d more result(s) - refine your search." % (len(trouves) - 100))
    return "\n".join(lignes)


def traiter_cherche_reseau(expediteur, commande, corps):
    """CHERCHE-RESEAU : on cherche chez nous, et on relaie la recherche a
    tous les serveurs connus. Ceux qui ont le fichier repondent directement
    au demandeur ; ceux qui ne l'ont pas se taisent."""
    terme = ""
    morceaux = commande.split(None, 1)
    if len(morceaux) == 2:
        reste = morceaux[1].strip()
        for prefixe in ("RESEAU ", "NETWORK "):
            if reste.upper().startswith(prefixe):
                reste = reste[len(prefixe):].strip()
        if reste.upper() in ("RESEAU", "NETWORK"):
            reste = ""
        terme = reste
    if not terme:
        for ligne in corps.splitlines():
            if ligne.strip():
                terme = ligne.strip()
                break
    if len(terme) < 2:
        envoyer(expediteur, "Recherche reseau / Network search",
                "Pour chercher dans tout le reseau, ecris le nom (ou un bout "
                "du nom) du fichier :\n"
                "- dans le sujet, apres la commande :  CHERCHE-RESEAU Dune\n"
                "- ou dans le corps du courriel, premiere ligne.\n\n"
                "To search the whole network, write the file name (or part "
                "of it):\n"
                "- in the subject, after the command:  SEARCH-NETWORK Dune\n"
                "- or in the email body, first line.\n")
        log("  -> cherche-reseau sans terme, mode d'emploi envoye")
        return
    pairs = charger_reseau()
    trouves = chercher(terme, catalogue())
    note = ("=== RECHERCHE RESEAU / NETWORK SEARCH : %s ===\n"
            "Resultats du serveur %s.\n\n" % (terme, NOM_SERVEUR))
    if trouves:
        corps_reponse = note + _corps_resultats(terme, trouves) + note_acces()
    else:
        corps_reponse = note + ("Aucun fichier trouve sur ce serveur.\n"
                                "No file found on this server.\n")
        if pairs:
            corps_reponse += ("\nLes autres serveurs cherchent aussi : ceux qui "
                              "ont le fichier vont te repondre directement.\n"
                              "The other servers are searching too: those with "
                              "the file will reply to you directly.\n")
    envoyer(expediteur, "Resultats / Results : %s - %s" % (terme, NOM_SERVEUR), corps_reponse)
    relais = 0
    for p in pairs:
        if p["courriel"]:
            try:
                envoyer(p["courriel"], "RELAIS-CHERCHE",
                        "Pour: %s\nRecherche: %s\n" % (expediteur, terme))
                relais += 1
            except Exception:
                pass
    log("  -> cherche-reseau %r : %d resultat(s) ici, relais a %d serveur(s)"
        % (terme, len(trouves), relais))
    log_reseau("CHERCHE-RESEAU %r : %d resultat(s) ici, relais a %d serveur(s)"
               % (terme, len(trouves), relais))


def traiter_relais_cherche(expediteur, corps):
    """Un pair nous relaie une recherche reseau. Meme garde-fou que
    RELAIS-LISTE : expediteur connu seulement. On repond au demandeur
    SEULEMENT si on a le fichier (silence = pas trouve ici)."""
    connus = [p["courriel"].lower() for p in charger_reseau()]
    if expediteur not in connus:
        log("  -> RELAIS-CHERCHE refuse (expediteur hors reseau) : %s" % expediteur)
        log_reseau("RELAIS-CHERCHE refuse (hors reseau) : %s" % expediteur)
        return
    pour = ""
    terme = ""
    for ligne in corps.splitlines():
        if ":" in ligne:
            k, v = ligne.split(":", 1)
            k = k.strip().lower()
            if k == "pour":
                pour = v.strip()
            elif k in ("recherche", "search"):
                terme = v.strip()
    if not pour or "@" not in pour or " " in pour or len(terme) < 2:
        log("  -> RELAIS-CHERCHE incomplet (Pour:/Recherche:), ignore")
        return
    trouves = chercher(terme, catalogue())
    if not trouves:
        log("  -> relais-cherche %r : rien ici, silence" % terme)
        log_reseau("RELAIS-CHERCHE %r : rien ici, silence" % terme)
        return
    note = ("=== RECHERCHE RESEAU / NETWORK SEARCH : %s ===\n"
            "Resultats du serveur %s (http://%s/), recherche passee par %s.\n"
            "Results from server %s, search relayed by %s.\n\n"
            % (terme, NOM_SERVEUR, mon_adresse(), expediteur, NOM_SERVEUR, expediteur))
    envoyer(pour, "Resultats / Results : %s - %s" % (terme, NOM_SERVEUR),
            note + _corps_resultats(terme, trouves) + note_acces())
    log("  -> relais-cherche %r : %d resultat(s) envoyes a %s" % (terme, len(trouves), pour))
    log_reseau("RELAIS-CHERCHE %r : %d resultat(s) envoyes a %s" % (terme, len(trouves), pour))


def note_reseau():
    """Entete commune des listes reseau (LISTE-RESEAU et RELAIS-LISTE) :
    le meme texte des deux cotes, avec le nom court du robot ('freq' /
    'freq2') et le nom du serveur. Plus d'adresse http:// dans l'entete."""
    court = COURRIEL.split("@")[0]
    note = "=== RESEAU FREQ ===   '%s'\nCette liste vient du serveur %s.\n" % (court, NOM_SERVEUR)
    pairs = charger_reseau()
    if pairs:
        note += ("Les %d autre(s) serveur(s) du reseau vont t'envoyer leur "
                 "propre liste directement par courriel.\n" % len(pairs))
        note += ("The %d other network server(s) will email you their own "
                 "list directly.\n" % len(pairs))
    return note + "\n"


def traiter_relais_liste(expediteur, corps):
    """Un pair nous demande d'envoyer notre liste a un demandeur (LISTE-RESEAU).
    Accepte SEULEMENT si l'expediteur est un serveur deja dans notre reseau :
    sinon n'importe qui pourrait faire envoyer nos listes a n'importe qui."""
    connus = [p["courriel"].lower() for p in charger_reseau()]
    if expediteur not in connus:
        log("  -> RELAIS-LISTE refuse (expediteur hors reseau) : %s" % expediteur)
        log_reseau("RELAIS-LISTE refuse (hors reseau) : %s" % expediteur)
        return
    pour = ""
    for ligne in corps.splitlines():
        if ":" in ligne:
            k, v = ligne.split(":", 1)
            if k.strip().lower() == "pour":
                pour = v.strip()
    if not pour or "@" not in pour or " " in pour:
        log("  -> RELAIS-LISTE sans adresse 'Pour:' valide, ignore")
        log_reseau("RELAIS-LISTE de %s : sans adresse 'Pour:' valide, ignore" % expediteur)
        return
    fichiers = catalogue()
    note = note_reseau()
    envoyer_liste(pour, fichiers,
                  "Liste des fichiers de %s (%d)" % (NOM_SERVEUR, len(fichiers)), note)
    log("  -> liste relais envoyee a %s (demande passee par %s)" % (pour, expediteur))
    log_reseau("RELAIS-LISTE : %d fichiers envoyes a %s (demande passee par %s)"
               % (len(fichiers), pour, expediteur))


def traiter_liste_reseau(expediteur):
    """LISTE-RESEAU : on envoie notre liste au demandeur, et on relaie sa
    demande a tous les serveurs connus, qui lui envoient la leur directement."""
    fichiers = catalogue()
    pairs = charger_reseau()
    note = note_reseau()
    envoyer_liste(expediteur, fichiers,
                  "Liste des fichiers de %s (%d)" % (NOM_SERVEUR, len(fichiers)), note)
    relais = 0
    for p in pairs:
        if p["courriel"]:
            try:
                envoyer(p["courriel"], "RELAIS-LISTE", "Pour: %s\n" % expediteur)
                relais += 1
            except Exception:
                pass
    log("  -> liste reseau envoyee (%d fichiers, relais a %d serveur(s))"
        % (len(fichiers), relais))
    log_reseau("LISTE-RESEAU de %s : %d fichiers envoyes, relais a %d serveur(s)"
               % (expediteur, len(fichiers), relais))


def reponse_serveurs():
    pairs = charger_reseau()
    lignes = ["Serveurs du reseau FREQ (%d) :" % (len(pairs) + 1), "",
              "1. %s (ce serveur)" % NOM_SERVEUR,
              "   Telechargement : http://%s/" % mon_adresse(),
              "   Robot courriel : %s" % COURRIEL, ""]
    for i, p in enumerate(pairs, 2):
        lignes.append("%d. %s" % (i, p["nom"]))
        lignes.append("   Telechargement : http://%s/" % p["adresse"])
        lignes.append("   Robot courriel : %s" % p["courriel"])
        if p["vu"]:
            lignes.append("   (vu le %s)" % p["vu"])
        lignes.append("")
    return "\n".join(lignes)


# ================= JETONS TEMPORAIRES (MODE_JETON) =================
# Un jeton = un mot de passe unique par demandeur, valide DUREE_JETON
# minutes. Plusieurs jetons sont valides en meme temps (un par usager) :
# personne ne bloque personne. Ils vivent en memoire : un redemarrage
# du robot les efface tous, les usagers re-demandent une liste.
_jetons = {}   # jeton -> heure d'expiration (time.time())
_verrou_jetons = threading.Lock()
_ALPHABET_JETON = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"  # sans caracteres ambigus (0/O, 1/l/I)
_a_surligner = None   # dernier jeton / mot de passe emis : a mettre en ROUGE VIF
                      # dans la version HTML du courriel (envoyer s'en sert)
_jeton_demande = None   # jeton de la demande en cours : genere une fois par
                        # demande (traiter), reutilise par les liens du
                        # courriel, son bloc d'acces et sa note d'acces —
                        # toutes les parties d'une liste partagent le meme.


def jeton_demande():
    """Retourne le jeton de la demande en cours, en le generant au besoin."""
    global _jeton_demande
    if _jeton_demande is None:
        _jeton_demande = nouveau_jeton()
    return _jeton_demande


def nouveau_jeton():
    global _a_surligner
    jeton = "".join(secrets.choice(_ALPHABET_JETON) for _ in range(10))
    maintenant = time.time()
    with _verrou_jetons:
        for j in [j for j, exp in _jetons.items() if exp <= maintenant]:
            del _jetons[j]   # menage des expires
        _jetons[jeton] = maintenant + DUREE_JETON * 60
    _a_surligner = jeton
    return jeton


def jeton_valide(mdp):
    if not mdp:
        return False
    with _verrou_jetons:
        expiration = _jetons.get(mdp)
    return expiration is not None and expiration > time.time()


def bloc_acces():
    """Lignes d'acces a imprimer dans les courriels (LISTE).
    En MODE_JETON : RIEN — les liens portent le jeton et marchent tout
    seuls, aucun texte d'acces a montrer. En mode mot de passe fixe,
    le bloc reste necessaire : l'usager doit le taper dans le navigateur."""
    if MODE_JETON:
        return []
    if HTTP_MOT_DE_PASSE:
        global _a_surligner
        _a_surligner = HTTP_MOT_DE_PASSE
        return ["=== ACCES AU TELECHARGEMENT / DOWNLOAD ACCESS ===",
                "Si le navigateur demande un nom d'utilisateur et un mot de passe :",
                "If the browser asks for a username and a password:",
                "  Nom d'utilisateur / Username : n'importe quoi / anything (ex. x)",
                "  Mot de passe / Password : " + HTTP_MOT_DE_PASSE,
                "", ""]
    return []


class ServeurHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Un client qui coupe sa connexion en plein transfert (10053/10054)
        # est normal : on l'ignore au lieu d'afficher un traceback complet.
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return
        log("Erreur HTTP : %r" % exc)


class HandlerTelechargement(SimpleHTTPRequestHandler):
    """Sert les fichiers et note chaque telechargement reussi dans les stats."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FTP_RACINE, **kwargs)

    def log_message(self, format, *args):
        # Journal d'acces du serveur (GET, codes 401/404, sondes de robots...) :
        # ecrit dans log/bot.log, RIEN dans la fenetre du .bat.
        try:
            ligne = "%s - - [%s] %s\n" % (self.address_string(),
                                          self.log_date_time_string(),
                                          format % args)
            with _verrou_log:
                with open(BOT_LOG, "a", encoding="utf-8") as f:
                    f.write(ligne)
        except Exception:
            pass

    def send_response(self, code, message=None):
        self._code_reponse = code
        super().send_response(code, message)

    def _basic_ok(self):
        """True si l'en-tete Authorization contient la cle maitre ou un
        jeton valide. Ne repond rien (l'appelant decide : 401 ou 404)."""
        entete = self.headers.get("Authorization", "")
        if not entete.startswith("Basic "):
            return False
        try:
            decode = base64.b64decode(entete[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        mdp = decode.split(":", 1)[1] if ":" in decode else ""
        if HTTP_MOT_DE_PASSE and mdp == HTTP_MOT_DE_PASSE:
            return True
        return jeton_valide(mdp)

    def _acces_autorise(self):
        """Modes SANS jeton : authentification Basic classique, avec
        demande de mot de passe (401) si refuse. En MODE_JETON on ne
        passe jamais par ici : le serveur est muet et repond 404."""
        if not HTTP_MOT_DE_PASSE and not MODE_JETON:
            return True
        if self._basic_ok():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Telechargement"')
        self.end_headers()
        return False

    def _chemin_jeton(self):
        """MODE_JETON (serveur muet) : traduit self.path en
        (chemin_fs, chemin_a_servir) quand le premier segment de
        l'adresse est un jeton valide ou la cle maitre — ou quand la
        cle maitre arrive en Basic Auth (l'acces direct du proprietaire).
        Sinon (None, None) : l'appelant repond 404, comme si rien
        n'existait a cette adresse."""
        brut = urlsplit(self.path)
        morceaux = [m for m in brut.path.split("/") if m]
        if morceaux and (jeton_valide(morceaux[0])
                         or (HTTP_MOT_DE_PASSE and morceaux[0] == HTTP_MOT_DE_PASSE)):
            if len(morceaux) < 2:
                return None, None
            servir = "/" + "/".join(morceaux[1:])
            if brut.query:
                servir += "?" + brut.query
            return self.translate_path("/" + "/".join(morceaux[1:])), servir
        if self._basic_ok():
            return self.translate_path(brut.path), self.path
        return None, None

    def list_directory(self, path):
        # Serveur muet : jamais de liste de dossier (fini le style
        # "partage Apache" ou on navigue dans l'arborescence).
        self.send_error(404)
        return None

    def do_HEAD(self):
        if MODE_JETON:
            chemin, servir = self._chemin_jeton()
            if chemin is None or os.path.isdir(chemin):
                self.send_error(404)
                return
            original = self.path
            self.path = servir
            try:
                super().do_HEAD()
            finally:
                self.path = original
        elif self._acces_autorise():
            if os.path.isdir(self.translate_path(self.path)):
                self.send_error(404)
                return
            super().do_HEAD()

    def _page_confirmation(self, chemin):
        """Petite page affichee au clic sur un lien : elle annonce le fichier,
        lance le telechargement tout seul apres 1 seconde (?dl=1),
        et dit au visiteur qu'il peut fermer la fenetre."""
        nom = os.path.basename(chemin)
        url_dl = html.escape(self.path + "?dl=1", quote=True)
        page = ("<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='1; url=" + url_dl + "'>"
                "<title>Telechargement</title></head>"
                "<body style='font-family:sans-serif; text-align:center; margin-top:12%;'>"
                "<h2>Fichier en telechargement :</h2>"
                "<p style='font-size:22px;'>" + html.escape(nom) + "</p>"
                "<p>Le telechargement demarre tout seul.<br>"
                "Tu peux fermer cette fenetre.</p>"
                "<p>Si rien ne demarre : <a href='" + url_dl + "'>clique ici</a></p>"
                "</body></html>")
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlsplit(self.path).path == "/freq-info.txt":
            # Page d'identite PUBLIQUE du serveur (aucun mot de passe demande) :
            # c'est elle qui permet de verifier qu'un vrai serveur FREQ repond
            # a une adresse, lors d'une inscription au reseau (ANNOUNCE).
            data = ("FREQ/1.0\nNom: %s\nCourriel: %s\n"
                    % (NOM_SERVEUR, COURRIEL)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if MODE_JETON:
            # Serveur muet : seul un lien a jeton valide (ou la cle
            # maitre) mene quelque part ; tout le reste = 404.
            chemin, servir = self._chemin_jeton()
            if chemin is None:
                self.send_error(404)
                return
        else:
            if not self._acces_autorise():
                return
            chemin = self.translate_path(self.path)
            servir = self.path
        # Jamais de navigation : un dossier ne montre rien, dans tous les modes.
        if os.path.isdir(chemin):
            self.send_error(404)
            return
        # Clic sur un lien de fichier (sans ?dl=1) -> page de confirmation d'abord.
        # Le vrai fichier part seulement avec ?dl=1 : c'est la que les stats comptent.
        if os.path.isfile(chemin) and "dl" not in parse_qs(urlsplit(self.path).query):
            self._page_confirmation(chemin)
            return
        original = self.path
        self.path = servir   # en mode jeton : le chemin SANS le jeton
        try:
            super().do_GET()
        finally:
            self.path = original
        if getattr(self, "_code_reponse", None) == 200 and os.path.isfile(chemin):
            enregistrer_telechargement(self.client_address[0],
                                       os.path.relpath(chemin, FTP_RACINE))


def chercher(terme, fichiers):
    """Cherche un fichier : correspondance exacte d'abord, puis partielle."""
    t = terme.strip().lower()
    exacts = [f for f in fichiers if os.path.basename(f).lower() == t]
    if exacts:
        return exacts
    return [f for f in fichiers if t in f.lower()]


def note_acces():
    """Note ajoutee aux reponses du robot quand le serveur est protege
    par un mot de passe FIXE : le demandeur le recoit automatiquement,
    pas besoin de contacter le proprietaire. En MODE_JETON : rien — les liens
    marchent tout seuls, aucune note necessaire."""
    if MODE_JETON:
        return ""
    if not HTTP_MOT_DE_PASSE:
        return ""
    global _a_surligner
    _a_surligner = HTTP_MOT_DE_PASSE
    return ("\n\nAcces au serveur de telechargement : si le navigateur demande un nom "
            "d'utilisateur et un mot de passe, entre n'importe quel nom "
            "d'utilisateur et le mot de passe : " + HTTP_MOT_DE_PASSE)


# Textes du haut (header.txt) et du bas (footer.txt) des listes,
# modifiables dans des fichiers a cote du script — on peut changer
# les mots sans toucher au code. Fichier absent : texte integre
# ci-dessous. Un changement de fichier est relu automatiquement
# (date de modification surveillee), pas besoin de relancer le .bat.
_ENTETE_DEFAUT = [
    "Astuce : envoie un courriel avec AIDE dans le sujet pour voir toutes les commandes.",
    "Tip: send an email with HELP in the subject to see all the commands.",
    "",
    "NOTE : si un fichier finit en .tgz, pas de panique : il s'ouvre avec",
    "WinRAR, comme les .rar (clic droit sur le fichier -> Extraire ici).",
    "NOTE: a file ending in .tgz opens with WinRAR, like .rar files",
    "(right-click the file -> Extract here).",
]
_PIED_DEFAUT = [
    "Pour telecharger : clique le lien (ca marche dans ton navigateur),",
    "ou reponds a ce courriel avec le nom du fichier dans le sujet.",
    "To download: click the link (works in your browser), or reply",
    "to this email with the file name in the subject.",
]
_cache_textes = {}   # nom de fichier -> (date de modification, lignes)


def _texte_lignes(nom, defaut):
    """Lignes de header.txt / footer.txt (a cote du script), ou le
    texte integre par defaut si le fichier n'existe pas."""
    chemin = os.path.join(DOSSIER_SCRIPT, nom)
    try:
        mt = os.path.getmtime(chemin)
    except OSError:
        return list(defaut)
    cache = _cache_textes.get(nom)
    if cache is None or cache[0] != mt:
        try:
            with open(chemin, encoding="utf-8") as f:
                lignes = f.read().splitlines()
        except OSError:
            return list(defaut)
        while lignes and not lignes[-1].strip():
            lignes.pop()
        _cache_textes[nom] = (mt, lignes)
        return list(lignes)
    return list(cache[1])


def reponse_liste(fichiers, acces=None):
    # En haut : header.txt (ou le texte integre). Tout en haut parce
    # que les gens ne lisent pas jusqu'en bas.
    lignes = _texte_lignes("header.txt", _ENTETE_DEFAUT)
    if lignes:
        lignes += [""]
    # Bloc d'acces : seulement en mode mot de passe fixe (en mode
    # jeton, les liens marchent tout seuls, rien a afficher).
    if not MODE_JETON:
        lignes += acces if acces is not None else bloc_acces()
    lignes += ["Voici les fichiers disponibles / Available files (%d) :" % len(fichiers), ""]
    for f in fichiers:
        lignes.append(f)
        lignes.append("   " + lien_http(f))
        lignes.append("")
    # En bas : footer.txt (ou le texte integre).
    lignes += _texte_lignes("footer.txt", _PIED_DEFAUT)
    return "\n".join(lignes)


def _limite_liste():
    """Taille max visee par courriel de liste, en octets. Le reglage
    LISTE_TAILLE_MAX_MO est borne entre 2 et 10 Mo : plus petit = trop de
    courriels, plus gros = ouverture lente et on s'approche de la limite
    de taille des boites courriel (20-25 Mo)."""
    return max(2, min(10, LISTE_TAILLE_MAX_MO)) * 1024 * 1024


def _taille_estimee(plain, html_octets):
    """Estimation de la taille FINALE du courriel (texte + HTML + encodage
    MIME + en-tetes). Mesure sur une vraie liste : (texte + HTML) x ~1,02."""
    return int((plain + html_octets) * 1.05) + 1024


def _tailles_entree(f):
    """Tailles (texte, HTML) de l'entree d'UN fichier dans la liste."""
    url = lien_http(f)
    plain = len((f + "\n   " + url + "\n\n").encode("utf-8"))
    h = len((html.escape(f) + "\n   " + '<a href="' + url + '">' + url
             + "</a>\n\n").encode("utf-8"))
    return plain, h


def decouper_liste(fichiers, acces, note_prefixe=""):
    """Decoupe la liste en morceaux dont la taille finale estimee reste
    sous la limite. On ne coupe jamais un fichier et son lien en deux."""
    base = note_prefixe + reponse_liste([], acces)
    p0 = len(base.encode("utf-8")) + 300        # + marge pour le bandeau PARTIE i/n
    h0 = (len(version_html(base, _a_surligner).encode("utf-8")) + 300) if _html_joint(base) else 0
    morceaux, courant, cp, ch = [], [], p0, h0
    for f in fichiers:
        pe, he = _tailles_entree(f)
        if not acces:
            he = 0
        if courant and _taille_estimee(cp + pe, ch + he) > _limite_liste():
            morceaux.append(courant)
            courant, cp, ch = [], p0, h0
        courant.append(f)
        cp += pe
        ch += he
    if courant or not morceaux:
        morceaux.append(courant)
    return morceaux


def envoyer_liste(destinataire, fichiers, sujet_base, note_prefixe=""):
    """Envoie une liste complete, en UN courriel si elle est assez petite,
    sinon en plusieurs courriels numerotes (sujet + ' - partie i/n').
    Le bloc d'acces est genere UNE fois : en mode jeton, toutes les
    parties portent le MEME jeton. Retourne le nombre de parties."""
    acces = bloc_acces()
    morceaux = decouper_liste(fichiers, acces, note_prefixe)
    total = len(morceaux)
    for i, bout in enumerate(morceaux, 1):
        bandeau = ""
        sujet = sujet_base
        if total > 1:
            bandeau = ("=== PARTIE %d / %d ===\n"
                       "Ceci est la partie %d de %d de la liste complete (%d fichiers).\n"
                       "This is part %d of %d of the full list (%d files).\n\n"
                       % (i, total, i, total, len(fichiers), i, total, len(fichiers)))
            sujet = "%s - partie %d/%d" % (sujet_base, i, total)
        else:
            bandeau = ("Ceci est la partie 1 de 1 de la liste complete (%d fichiers).\n"
                       "This is part 1 of 1 of the full list (%d files).\n\n"
                       % (len(fichiers), len(fichiers)))
        envoyer(destinataire, sujet,
                note_prefixe + bandeau + reponse_liste(bout, acces))
    return total


def reponse_aide():
    return (
        "Bonjour / Hello,\n\n"
        "Ce service envoie des fichiers sur demande par courriel : tu demandes,\n"
        "tu recois un lien de telechargement.\n"
        "This service sends files on email request: you ask, you get a download link.\n\n"
        "Commandes — a mettre dans le SUJET du courriel\n"
        "Commands — put them in the email SUBJECT\n\n"
        "  LISTE ou/or LIST\n"
        "      -> la liste des fichiers de ce serveur / this server's file list\n"
        "  LISTE-RESEAU ou/or LIST-NETWORK\n"
        "      -> les listes de TOUS les serveurs du reseau (chacun t'envoie la sienne)\n"
        "         / every network server's list (each one emails you its own)\n"
        "  CHERCHE-RESEAU <nom> ou/or SEARCH-NETWORK <name>\n"
        "      -> cherche un fichier dans TOUT le reseau (le nom va apres la commande,\n"
        "         ou seul dans le corps du courriel) / searches a file across the WHOLE\n"
        "         network (name goes after the command, or alone in the email body)\n"
        "  <nom du fichier / file name>\n"
        "      -> le lien de telechargement direct / the direct download link\n"
        "         (un bout du nom suffit / part of the name is enough, ex : Dune)\n"
        "  SERVEURS ou/or SERVERS\n"
        "      -> la liste des serveurs du reseau FREQ / the FREQ network servers\n"
        "  AIDE ou/or HELP\n"
        "      -> ce message / this message\n"
    ) + note_acces()


def version_html(corps, cible):
    """Version HTML du courriel : le MEME texte, en <pre> (mise en page
    identique au texte brut), avec deux differences :
    - les adresses http://, https:// et ftp:// deviennent de VRAIS liens
      <a href=...> — sans ca, dans du HTML, une adresse ecrite en texte
      n'est PAS cliquable (c'est le client texte qui les rend cliquables
      tout seul, pas le HTML) ;
    - le jeton / mot de passe est affiche en ROUGE VIF, gras et plus gros."""
    h = html.escape(corps)
    # 1) Les liens d'abord : chaque adresse devient une ancre cliquable.
    h = re.sub(r'((?:https?|ftp)://[^\s<]+)',
               r'<a href="\1">\1</a>', h)
    # 2) Le rouge ensuite, seulement dans le texte visible (jamais dans
    #    une balise, donc jamais dans un href), et seulement le code
    #    entier (pas un bout d'un autre mot).
    c = html.escape(cible)
    motif = re.compile(r'(?<![A-Za-z0-9])' + re.escape(c) + r'(?![A-Za-z0-9])')
    surligne = ('<span style="color:#ff0000; font-weight:bold; font-size:140%;">'
                + c + '</span>')
    morceaux = re.split(r'(<[^>]+>)', h)
    dans_lien = False
    for i, morceau in enumerate(morceaux):
        if i % 2 == 1:   # indices impairs = balises : on suit les <a>...</a>
            if morceau.startswith("<a "):
                dans_lien = True
            elif morceau == "</a>":
                dans_lien = False
        elif not dans_lien:   # texte visible hors liens seulement
            morceaux[i] = motif.sub(surligne, morceau)
    h = "".join(morceaux)
    return ("<html><body><pre style=\"font-family:monospace; white-space:pre-wrap;\">"
            + h + "</pre></body></html>")


def _html_joint(corps):
    """La version HTML (code en rouge vif) est jointe seulement quand un
    code est affiche EN TEXTE dans le courriel — donc en mode mot de
    passe fixe. En MODE_JETON, le code n'existe que DANS les liens :
    pas de HTML, et le courriel reste deux fois plus leger (les clients
    courriel rendent les liens du texte brut cliquables tout seuls)."""
    return bool(_a_surligner) and not MODE_JETON and _a_surligner in corps


def envoyer(destinataire, sujet, corps):
    # Un sujet ne doit JAMAIS contenir de saut de ligne (v2.4) : un sujet
    # replie par le client courriel, ou une chaine "Introuvable : ..." qui
    # grandit a chaque reponse, plantait l'envoi (ValueError linefeed).
    sujet = " ".join(str(sujet).split())
    m = EmailMessage()
    m["From"] = COURRIEL
    m["To"] = destinataire
    m["Subject"] = sujet
    m.set_content(corps)
    # Si ce courriel affiche un mot de passe a surligner, ajouter la
    # version HTML ou il saute aux yeux en rouge vif. Le client courriel
    # du destinataire choisit : HTML si possible, sinon le texte brut.
    if _html_joint(corps):
        m.add_alternative(version_html(corps, _a_surligner), subtype="html")
    with smtplib.SMTP(SMTP_SERVEUR, SMTP_PORT, timeout=30) as s:
        if SMTP_STARTTLS:
            s.starttls()
        s.login(COURRIEL, MOT_DE_PASSE)
        s.send_message(m)


def traiter(msg_brut):
    msg = message_from_bytes(msg_brut)
    expediteur = parseaddr(msg.get("From", ""))[1].lower()
    sujet = decodage(msg.get("Subject", ""))

    # Anti-boucle : ne jamais se repondre a soi-meme
    if not expediteur or expediteur == COURRIEL.lower():
        return
    # Liste blanche optionnelle
    if EXPEDITEURS_OK and expediteur not in [e.lower() for e in EXPEDITEURS_OK]:
        log("Ignore (expediteur non autorise) : " + expediteur)
        return

    # Un jeton par demande : les liens de TOUTES les reponses a ce
    # courriel le porteront (et toutes les parties d'une liste aussi).
    global _jeton_demande
    _jeton_demande = nouveau_jeton() if MODE_JETON else None

    # Nettoyer les prefixes Re: / Fwd:, puis TOUS les sauts de ligne et
    # espaces multiples (v2.4) : un sujet replie arrivait avec un vrai
    # saut de ligne dedans, et "Introuvable : " + ce sujet plantait
    # l'envoi suivant. Apres ce nettoyage, plus aucun sujet ne peut
    # contenir de saut de ligne.
    commande = " ".join(sujet.split())
    while commande.lower().startswith(("re:", "fwd:", "tr:")):
        commande = " ".join(commande.split(":", 1)[1].split())
    # Sujet vide -> prendre la premiere ligne du corps
    if not commande:
        for ligne in corps_texte(msg).splitlines():
            if ligne.strip():
                commande = ligne.strip()
                break

    mot = commande.upper()
    log("Demande de %s : %r" % (expediteur, commande))
    # Anti-boucle pairs (v2.4) : un robot du reseau ne doit JAMAIS etre
    # traite comme un demandeur ordinaire. Ses listes, ses "Introuvable"
    # et ses reponses qui retombent dans notre boite ne sont pas des
    # commandes : on les ignore, sans repondre, sinon chaque liste
    # relance une reponse "Introuvable" qui relance une autre reponse...
    # Seules les VRAIES commandes reseau d'un pair sont acceptees.
    _commandes_reseau = ("LISTE-RESEAU", "LIST-RESEAU", "LISTE RESEAU",
                         "LIST-NETWORK", "NETWORK-LIST", "LIST NETWORK",
                         "LISTE-NETWORK", "RELAIS-LISTE", "RELAY-LIST",
                         "RELAIS LISTE", "RELAY LIST", "RELAIS-CHERCHE",
                         "RELAY-SEARCH", "ANNOUNCE", "ANNONCE",
                         "RESEAU", "NETWORK", "SERVEURS", "SERVERS")
    _pairs = [p["courriel"].lower() for p in charger_reseau()]
    if expediteur in _pairs and not (
            mot in _commandes_reseau
            or mot.startswith(("ANNOUNCE ", "ANNONCE ",
                               "CHERCHE-RESEAU ", "RECHERCHE-RESEAU ",
                               "SEARCH-NETWORK "))):
        log("  -> ignore (pair reseau, pas une commande reseau) : %r" % commande)
        log_reseau("Ignore (pair %s, pas une commande reseau) : %r" % (expediteur, commande))
        return

    if mot in ("LISTE", "LIST"):
        fichiers = catalogue()
        n = envoyer_liste(expediteur, fichiers, "Liste des fichiers (%d)" % len(fichiers))
        log("  -> liste envoyee (%d fichiers, %d partie(s))" % (len(fichiers), n))
    elif mot in ("SERVEURS", "SERVERS"):
        envoyer(expediteur, "Serveurs du reseau FREQ", reponse_serveurs())
        log("  -> liste des serveurs envoyee")
    elif mot in ("LISTE-RESEAU", "LIST-RESEAU", "LISTE RESEAU",
                 "LIST-NETWORK", "NETWORK-LIST", "LIST NETWORK", "LISTE-NETWORK"):
        traiter_liste_reseau(expediteur)
    elif mot in ("RELAIS-LISTE", "RELAY-LIST", "RELAIS LISTE", "RELAY LIST"):
        traiter_relais_liste(expediteur, corps_texte(msg))
    elif (mot in ("CHERCHE-RESEAU", "RECHERCHE-RESEAU", "SEARCH-NETWORK",
                  "CHERCHE RESEAU", "RECHERCHE RESEAU", "SEARCH NETWORK")
          or mot.startswith(("CHERCHE-RESEAU ", "RECHERCHE-RESEAU ", "SEARCH-NETWORK "))):
        traiter_cherche_reseau(expediteur, commande, corps_texte(msg))
    elif mot in ("RELAIS-CHERCHE", "RELAY-SEARCH"):
        traiter_relais_cherche(expediteur, corps_texte(msg))
    elif mot == "ANNOUNCE" or mot == "ANNONCE" or mot.startswith("ANNOUNCE ") or mot.startswith("ANNONCE "):
        # Le corps + le sujet : l'adresse peut etre ecrite a l'un ou l'autre endroit
        traiter_announce(expediteur, corps_texte(msg) + "\n" + commande)
    elif mot == "RESEAU" or mot == "NETWORK":
        traiter_reseau(corps_texte(msg))
    elif mot in ("AIDE", "HELP", ""):
        envoyer(expediteur, "Aide - service de fichiers", reponse_aide())
        log("  -> aide envoyee")
    else:
        trouves = chercher(commande, catalogue())
        if len(trouves) == 1:
            f = trouves[0]
            corps = ("Voici ton fichier :\n\n%s\n\n"
                     "Telecharger (navigateur) :\n%s\n\n"
                     "Telecharger (client FTP, avec reprise du telechargement) :\n%s\n"
                     % (f, lien_http(f), lien_ftp(f))) + note_acces()
            envoyer(expediteur, "Ton fichier : " + os.path.basename(f), corps)
            log("  -> lien envoye : " + f)
        elif len(trouves) > 1:
            lignes = ["Plusieurs fichiers correspondent a \"%s\" :" % commande, ""]
            for f in trouves:
                lignes.append(f)
                lignes.append("   " + lien_http(f))
                lignes.append("")
            lignes.append("Reponds avec le nom exact pour en choisir un.")
            envoyer(expediteur, "Resultats pour : " + commande, "\n".join(lignes) + note_acces())
            log("  -> %d resultats envoyes" % len(trouves))
        else:
            corps = ("Aucun fichier trouve pour \"%s\".\n\n"
                     "Reponds avec LISTE dans le sujet pour voir tous les fichiers disponibles."
                     % commande)
            envoyer(expediteur, "Introuvable : " + commande, corps)
            log("  -> introuvable")


_MOIS_IMAP = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_dernier_menage = 0.0


def menage_boite(boite):
    """Menage automatique de la boite : supprime les courriels DEJA
    TRAITES (drapeau 'lu') plus vieux que GARDER_JOURS jours.
    Au plus une fois par heure. Les courriels pas encore traites
    ne sont jamais touches."""
    global _dernier_menage
    if not NETTOYAGE_BOITE:
        return
    maintenant = time.time()
    if maintenant - _dernier_menage < 3600:
        return
    _dernier_menage = maintenant
    if GARDER_JOURS <= 0:
        critere = "SEEN"
    else:
        limite = time.localtime(maintenant - GARDER_JOURS * 86400)
        critere = "SEEN BEFORE %02d-%s-%04d" % (
            limite.tm_mday, _MOIS_IMAP[limite.tm_mon - 1], limite.tm_year)
    typ, donnees = boite.search(None, critere)
    if typ != "OK" or not donnees or not donnees[0]:
        return
    numeros = donnees[0].split()
    for num in numeros:
        boite.store(num, "+FLAGS", "\\Deleted")
    boite.expunge()
    log("Menage de la boite : %d courriel(s) traite(s) de plus de %d jour(s) supprime(s)."
        % (len(numeros), GARDER_JOURS))


def verifier_boite():
    if IMAP_SSL:
        boite = imaplib.IMAP4_SSL(IMAP_SERVEUR, IMAP_PORT)
    else:
        boite = imaplib.IMAP4(IMAP_SERVEUR, IMAP_PORT)
    try:
        boite.login(COURRIEL, MOT_DE_PASSE)
        boite.select("INBOX")
        _, numeros = boite.search(None, "UNSEEN")
        for num in numeros[0].split():
            _, data = boite.fetch(num, "(RFC822)")   # le fetch marque le message comme lu
            try:
                traiter(data[0][1])
            except Exception as e:
                log("ERREUR sur un message : %r" % e)
        try:
            menage_boite(boite)
        except Exception as e:
            log("Menage de la boite : echec (%r)" % e)
    finally:
        try:
            boite.logout()
        except Exception:
            pass


def main():
    if not os.path.isdir(FTP_RACINE):
        print("ERREUR : le dossier FTP_RACINE n'existe pas : " + FTP_RACINE)
        sys.exit(1)
    log("Mailbot FTP v%s demarre. Boite : %s | Racine FTP : %s" % (VERSION, COURRIEL, FTP_RACINE))
    log("Configuration : %s" % (CONFIG_SOURCE if CONFIG_SOURCE
                                else "valeurs integrees au script (aucun config.conf)"))
    if not 2 <= LISTE_TAILLE_MAX_MO <= 10:
        log("LISTE_TAILLE_MAX_MO = %s : borne a %d Mo (minimum 2, maximum 10)"
            % (LISTE_TAILLE_MAX_MO, _limite_liste() // (1024 * 1024)))
    if NETTOYAGE_BOITE:
        log("Nettoyage de la boite actif : les courriels traites sont gardes %d jour(s)"
            % GARDER_JOURS)
    res_purge = purger_journal(BOT_LOG, PURGE_JOURS)
    if res_purge:
        log("Purge journal : %d ligne(s) gardee(s), %d effacee(s) (%d derniers jours)"
            % (res_purge[0], res_purge[1], PURGE_JOURS))
    res_purge_reseau = purger_journal(RESEAU_LOG, PURGE_JOURS)
    if res_purge_reseau:
        log("Purge journal reseau : %d ligne(s) gardee(s), %d effacee(s) (%d derniers jours)"
            % (res_purge_reseau[0], res_purge_reseau[1], PURGE_JOURS))
    log("Journal reseau : %s" % RESEAU_LOG)
    for _p in charger_reseau():
        if _p["nom"] == NOM_SERVEUR:
            log("ATTENTION : le pair %s (%s) porte le meme nom que ce serveur : "
                "change NOM_SERVEUR dans son config pour un nom unique" % (_p["nom"], _p["adresse"]))
            log_reseau("ATTENTION : pair %s (%s) meme nom que ce serveur" % (_p["nom"], _p["adresse"]))
    demarrer_http()
    while True:
        try:
            verifier_boite()
        except Exception as e:
            log("ERREUR connexion : %r (nouvel essai dans %ds)" % (e, DELAI_VERIF))
        time.sleep(DELAI_VERIF)


if __name__ == "__main__":
    main()
