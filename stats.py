# stats.py — Statistiques HTML des serveurs, a partir de leurs logs.
# VERSION = "1.4"  (regle MAJEUR.MINEUR : +1 majeur = nouvelle fonction ou
#   v1.3 : les visites Vendres datees (depuis vendres v3.6) sont lues avec
#   leur jour et leur heure ; l'ancien format sans date reste lu.
#   v1.4 : legende des codes de statut (colonne Signification) + une
#   page detail par code, ouverte dans un nouvel onglet quand on
#   clique le code (pages et IPs qui ont donne ce code).
# changement de comportement, +1 mineur = correctif ou texte ou reglage)
#
# Script AUTONOME : il ne touche a aucun autre .py, il ne fait que LIRE
# les fichiers de logs indiques dans stats-config.conf et ecrire des
# pages HTML dans DOSSIER_SORTIE. Aucun serveur, aucune modification
# des logs, aucune dependance externe (bibliotheque standard seulement).
#
# Comme AWStats : on lance le script, il regenere les pages. Ce n'est
# PAS du temps reel : les chiffres datent de la derniere execution.
# Relancer = rafraichir.
#
# Formats de logs reconnus :
#   - "IP METHODE /chemin" (vendres.log ancien format : pas de date, pas de code)
#   - "AAAA-MM-JJ HH:MM:SS IP METHODE /chemin" (vendres.log depuis la v3.6)
#   - "IP - - [25/Oct/2026 13:49:04] \"GET /chemin HTTP/1.1\" 200 -"
#     (mini-web.log et bot.log : format Apache, avec date, code, octets.
#     La date peut etre "25/Oct/2026 13:49:04" sans fuseau, ou avec
#     fuseau "25/Oct/2026:13:49:04 -0400". Les lignes "code 404, message"
#     ne sont pas des visites : elles sont ignorees, pas comptees.)
# Les autres lignes (demarrage, messages) sont ignorees et comptees.
#
# Limites honnetes, dues aux logs eux-memes (pas au script) :
#   - Sources de trafic (referents) : IMPOSSIBLE, aucun log ne les note.
#   - Navigateurs / appareils : IMPOSSIBLE, aucun log ne note le user-agent.
#   - Vendres : pas de dates ni de codes dans vendre.log, donc pas de
#     sections Jour / Heure / Statuts pour ce serveur.
#   - Pays et FAI (qui possede l'IP) : via ip-api.com pour les IPs les
#     plus frequentes, resultats gardes dans geo-cache.json (effacer ce
#     fichier pour forcer une nouvelle resolution). IPs privees ignorees.
#   - Sondes : detection par motifs dans le chemin (php, asp, admin...),
#     c'est un indicateur, pas une certitude.
#
# Utilisation : python stats.py [stats-config.conf]
#   Sans config a cote du script, un modele est cree et le script s'arrete.

import csv
import html
import json
import os
import re
import socket
import sys
import time
import urllib.request
from collections import Counter

VERSION = "1.4"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "stats-config.conf")
GEO_CACHE_FILE = os.path.join(SCRIPT_DIR, "geo-cache.json")

MOIS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
# Legende des codes de statut HTTP (v1.4) : un code inconnu affiche
# "Code non repertorie" au lieu de rien, pour ne pas laisser un blanc.
LEGENDE_STATUTS = {
    "100": "Continue — le serveur attend la suite de la requete",
    "101": "Changement de protocole",
    "200": "OK — page servie normalement",
    "201": "Cree — ressource creee",
    "202": "Accepte — requete acceptee, traitement en cours",
    "203": "Infos non officielles",
    "204": "Pas de contenu",
    "205": "Reinitialiser le contenu",
    "206": "Contenu partiel",
    "300": "Choix multiples",
    "301": "Deplace definitivement",
    "302": "Trouve (redirection temporaire)",
    "303": "Voir ailleurs",
    "304": "Non modifie — deja en cache, pas re-telecharge",
    "305": "Utiliser un proxy",
    "307": "Redirection temporaire",
    "308": "Redirection permanente",
    "400": "Mauvaise requete",
    "401": "Non autorise — authentification requise",
    "402": "Paiement requis",
    "403": "Interdit — acces refuse",
    "404": "Introuvable — la page n'existe pas",
    "405": "Methode non autorisee",
    "406": "Non acceptable",
    "407": "Authentification proxy requise",
    "408": "Delai de requete depasse",
    "409": "Conflit",
    "410": "Disparu definitivement",
    "411": "Longueur requise",
    "412": "Precondition echouee",
    "413": "Contenu trop gros",
    "414": "Adresse trop longue",
    "415": "Type de contenu non supporte",
    "416": "Plage demandee impossible",
    "417": "Attente echouee",
    "418": "C'est une theiere",
    "421": "Requete mal dirigee",
    "422": "Entite impossible a traiter",
    "423": "Verrouille",
    "424": "Echec de dependance",
    "425": "Trop tot",
    "426": "Mise a niveau requise",
    "428": "Precondition requise",
    "429": "Trop de requetes",
    "431": "En-tetes de requete trop gros",
    "451": "Indisponible pour raisons legales",
    "500": "Erreur interne du serveur",
    "501": "Non implemente — methode non supportee par le serveur",
    "502": "Mauvaise passerelle",
    "503": "Service indisponible (surcharge ou maintenance)",
    "504": "Delai de passerelle depasse",
    "505": "Version HTTP non supportee",
    "506": "Variante negocie aussi",
    "507": "Stockage insuffisant",
    "508": "Boucle detectee",
    "511": "Authentification reseau requise",
}


def legende_statut(code):
    """Signification d'un code, ou 'Code non repertorie' si inconnu."""
    return LEGENDE_STATUTS.get(str(code), "Code non repertorie")
# Mois en toutes lettres : pas de strptime %b, qui depend de la langue
# de Windows et casserait sur un systeme en francais ("Oct" vs "oct.").

RE_CLF = re.compile(
    r'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4})[: ](\d{2}):(\d{2}):(\d{2})(?: [^\]]+)?\] '
    r'"(\S+) (\S+) HTTP/[^"]*" (\d{3}) (\S+)')
RE_SIMPLE = re.compile(r'^(\S+) (GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) (\S+)$')
# Visites Vendres depuis la v3.6 : date devant chaque visite.
RE_VENDRES_DATE = re.compile(r'^(\d{4})-(\d{2})-(\d{2}) (\d{2}):\d{2}:\d{2} (\S+) (GET|POST|HEAD|PUT|DELETE|OPTIONS|PATCH) (\S+)$')
# Un jeton FREQ = 10 caracteres de l'alphabet sans ambigus (pas de 0/O/1/l/I).
RE_JETON = re.compile(r'^/[ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789]{10}(/.*)?$')

# Motifs de sondes de robots dans le chemin (minuscules).
MOTIFS_SONDES = [".php", ".asp", ".jsp", ".cfm", ".pl", ".cgi", ".jsa",
                 ".jhtml", ".shtml", ".env", ".bak", "/admin", "/wp-",
                 "/sdk", "/openapi", "/swagger", "/api-docs", "/.git",
                 "/hnap", "/dana-", "/confluence", "/cfide", "/evox",
                 "/+cscoe+", "nmaplowercheck", "trinity.txt", "/menu.",
                 "/base.", "/localstart", "/inicio", "/indice", "/start.",
                 "/home.", "/default.", "/main.", "/login.", "/userinfos"]

MODELE_CONFIG = """# stats-config.conf — Statistiques HTML des serveurs (script autonome).
# Une ligne = un reglage NOM = valeur. Les lignes # sont ignorees.
# DOSSIER_SORTIE : ou les pages HTML sont ecrites (cree si absent).
# TOP_N : nombre de lignes dans chaque classement.
# GEOIP : oui = pays et FAI des IPs les plus frequentes via ip-api.com
#   (resultats gardes dans geo-cache.json) ; non = noms d'hotes seulement.
# Chaque serveur : un NOM, le chemin de son LOG, et le CSV des
#   telechargements si le serveur en a un (vide sinon).
# Un chemin vide = serveur ignore. Les logs ne sont jamais modifies.

DOSSIER_SORTIE = D:/Stats
TOP_N = 30
GEOIP = oui

SERVEUR_1_NOM = Vendres
SERVEUR_1_LOG = D:/Serveurs/Vendres/vendres.log
SERVEUR_1_CSV =

SERVEUR_2_NOM = FREQ1-Files
SERVEUR_2_LOG =
SERVEUR_2_CSV =

SERVEUR_3_NOM = Fidonet
SERVEUR_3_LOG =
SERVEUR_3_CSV =

SERVEUR_4_NOM = MiniWeb
SERVEUR_4_LOG =
SERVEUR_4_CSV =
"""


def lire_config(chemin):
    """Lit un config.conf style NOM = valeur, # = commentaire."""
    cfg = {}
    if not os.path.isfile(chemin):
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(MODELE_CONFIG)
        print("Config creee : %s" % chemin)
        print("Remplis les chemins des logs puis relance le script.")
        return None
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            nom, val = ligne.split("=", 1)
            cfg[nom.strip()] = val.strip()
    return cfg


def liste_serveurs(cfg):
    """Regroupe les SERVEUR_N_* en liste ordonnee."""
    serveurs = []
    for n in range(1, 10):
        nom = cfg.get("SERVEUR_%d_NOM" % n, "")
        log = cfg.get("SERVEUR_%d_LOG" % n, "")
        csv_f = cfg.get("SERVEUR_%d_CSV" % n, "")
        if nom and (log or csv_f):
            serveurs.append({"nom": nom, "log": log, "csv": csv_f})
    return serveurs


def est_privee(ip):
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            deux = int(ip.split(".")[1])
            if 16 <= deux <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


def normaliser_chemin(chemin):
    """Sans la requete (?...) ; jeton FREQ remplace par <jeton>."""
    chemin = chemin.split("?", 1)[0]
    m = RE_JETON.match(chemin)
    if m:
        return "/<jeton>" + (m.group(1) or "")
    return chemin


def est_sonde(chemin):
    c = chemin.lower()
    return any(motif in c for motif in MOTIFS_SONDES)


def analyser_log(chemin):
    """Parse un log, retourne les compteurs et les infos de periode."""
    st = {"hits": 0, "ignorees": 0, "pages": Counter(), "ips": Counter(),
          "statuts": Counter(), "par_statut_pages": {},
          "par_statut_ips": {}, "octets": 0, "a_octets": False,
          "par_jour": Counter(), "par_heure": Counter(),
          "sondes": Counter(), "nb_sondes": 0,
          "premier": None, "dernier": None}
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        for ligne in f:
            ligne = ligne.rstrip("\n")
            m = RE_CLF.match(ligne)
            if m:
                ip = m.group(1)
                jour = "%s-%02d-%s" % (m.group(4), MOIS.get(m.group(3), 0), m.group(2))
                heure = m.group(5)
                methode = m.group(8)
                chemin_p = normaliser_chemin(m.group(9))
                statut = m.group(10)
                taille = m.group(11)
                st["hits"] += 1
                st["ips"][ip] += 1
                st["pages"][chemin_p] += 1
                st["statuts"][statut] += 1
                st["par_statut_pages"].setdefault(statut, Counter())[chemin_p] += 1
                st["par_statut_ips"].setdefault(statut, Counter())[ip] += 1
                if taille != "-":
                    try:
                        st["octets"] += int(taille)
                        st["a_octets"] = True
                    except ValueError:
                        pass
                if MOIS.get(m.group(3)):
                    st["par_jour"][jour] += 1
                    st["par_heure"][heure] += 1
                    if st["premier"] is None:
                        st["premier"] = jour
                    st["dernier"] = jour
                if est_sonde(chemin_p):
                    st["sondes"][chemin_p] += 1
                    st["nb_sondes"] += 1
                continue
            m = RE_VENDRES_DATE.match(ligne)
            if m:
                jour = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
                heure = m.group(4)
                ip, methode, chemin_p = m.group(5), m.group(6), normaliser_chemin(m.group(7))
                st["hits"] += 1
                st["ips"][ip] += 1
                st["pages"][chemin_p] += 1
                st["par_jour"][jour] += 1
                st["par_heure"][heure] += 1
                if st["premier"] is None:
                    st["premier"] = jour
                st["dernier"] = jour
                if est_sonde(chemin_p):
                    st["sondes"][chemin_p] += 1
                    st["nb_sondes"] += 1
                continue
            m = RE_SIMPLE.match(ligne)
            if m:
                ip, methode, chemin_p = m.group(1), m.group(2), normaliser_chemin(m.group(3))
                st["hits"] += 1
                st["ips"][ip] += 1
                st["pages"][chemin_p] += 1
                if est_sonde(chemin_p):
                    st["sondes"][chemin_p] += 1
                    st["nb_sondes"] += 1
                continue
            st["ignorees"] += 1
    return st


def analyser_csv(chemin):
    """CSV telechargements FREQ : Date;IP;Fichier -> compteurs."""
    dl = {"total": 0, "fichiers": Counter(), "ips": Counter()}
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace", newline="") as f:
            for ligne in csv.reader(f, delimiter=";"):
                if len(ligne) != 3 or ligne[0] == "Date":
                    continue
                _date, ip, fichier = ligne
                dl["total"] += 1
                dl["fichiers"][fichier] += 1
                dl["ips"][ip] += 1
    except OSError:
        pass
    return dl


def charger_cache_geo():
    try:
        with open(GEO_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sauver_cache_geo(cache):
    try:
        with open(GEO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def enrichir_ips(top_ips, geoip):
    """Noms d'hotes (DNS inverse) + pays/FAI (ip-api, top 50, cache)."""
    cache = charger_cache_geo()
    infos = {}
    nouveaux = 0
    for ip, _hits in top_ips[:50]:
        entree = cache.get(ip)
        if entree is None:
            entree = {"host": "", "country": "", "isp": "", "org": ""}
            if not est_privee(ip):
                try:
                    entree["host"] = socket.gethostbyaddr(ip)[0]
                except Exception:
                    entree["host"] = ""
                if geoip:
                    try:
                        req = urllib.request.Request(
                            "http://ip-api.com/json/%s?fields=status,country,isp,org,as,query" % ip,
                            headers={"User-Agent": "stats-py/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as rep:
                            don = json.load(rep)
                        if don.get("status") == "success":
                            entree["country"] = don.get("country", "")
                            entree["isp"] = don.get("isp", "") or don.get("org", "")
                            entree["org"] = don.get("as", "")
                    except Exception:
                        pass
                    time.sleep(1.4)
            cache[ip] = entree
            nouveaux += 1
        infos[ip] = entree
    if nouveaux:
        sauver_cache_geo(cache)
    return infos


def pct(n, total):
    return ("%.1f %%" % (100.0 * n / total)) if total else "0 %"


def taille_humaine(octets):
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024:
            return "%.1f %s" % (octets, unite) if unite != "o" else "%d o" % octets
        octets /= 1024.0
    return "%.1f To" % octets


def tableau_lignes(titre, lignes, entetes):
    """lignes = liste de tuples de cellules deja echappees."""
    if not lignes:
        return ""
    out = ["<h2>%s</h2>" % html.escape(titre), "<table><tr>"]
    for ent in entetes:
        out.append("<th>%s</th>" % html.escape(ent))
    out.append("</tr>")
    for ligne in lignes:
        out.append("<tr>" + "".join("<td>%s</td>" % c for c in ligne) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


CSS = """
body{background:#0d0d0d;color:#e8ecf4;font-family:Arial,sans-serif;margin:0}
.wrap{max-width:1000px;margin:0 auto;padding:20px}
h1{letter-spacing:2px}
h2{color:#7dd3fc;margin-top:32px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #333;padding:6px 10px;text-align:left}
th{background:#1f2937;color:#7dd3fc}
td.num{text-align:right}
.cartes{display:flex;gap:12px;flex-wrap:wrap}
.carte{background:#1f2937;border-radius:8px;padding:12px 18px;min-width:130px}
.carte b{font-size:22px;display:block;color:#7dd3fc}
.note{color:#9aa3b2;font-size:13px}
a{color:#7dd3fc}
"""

BASE_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>%s</title><style>%s</style></head>
<body><div class="wrap">
%s
<p class="note">Page generee le %s par stats.py v%s — relancer le script pour rafraichir.</p>
</div></body></html>
"""


def page_serveur(srv, st, dl, infos, top_n, geoip):
    nom = srv["nom"]
    total = st["hits"]
    corps = ["<h1>Statistiques — %s</h1>" % html.escape(nom)]
    if st["premier"]:
        corps.append("<p>Periode couverte : du %s au %s.</p>" % (st["premier"], st["dernier"]))
    else:
        corps.append("<p class=\"note\">Ce log ne contient pas de dates : "
                     "pas de repartition par jour ou par heure.</p>")
    corps.append("<p class=\"note\">Log : %s — lignes lues : %d, ignorees : %d.</p>"
                 % (html.escape(srv["log"]), total + st["ignorees"], st["ignorees"]))
    cartes = [("Hits", "%d" % total),
              ("IPs uniques", "%d" % len(st["ips"])),
              ("Pages uniques", "%d" % len(st["pages"]))]
    if st["a_octets"]:
        cartes.append(("Octets servis", taille_humaine(st["octets"])))
    if dl:
        cartes.append(("Telechargements", "%d" % dl["total"]))
    if st["nb_sondes"]:
        cartes.append(("Sondes detectees", "%d" % st["nb_sondes"]))
    corps.append("<div class=\"cartes\">" + "".join(
        "<div class=\"carte\"><b>%s</b>%s</div>" % (v, k) for k, v in cartes) + "</div>")
    corps.append(tableau_lignes("Pages les plus demandees",
                 [(html.escape(p), "<span class=\"num\">%d</span>" % n, pct(n, total))
                  for p, n in st["pages"].most_common(top_n)],
                 ["Page", "Hits", "%"]))
    top_ips = st["ips"].most_common(top_n)
    lignes_ips = []
    for ip, n in top_ips:
        inf = infos.get(ip, {})
        lignes_ips.append((html.escape(ip), "<span class=\"num\">%d</span>" % n,
                           html.escape(inf.get("host", "")),
                           html.escape(inf.get("country", "")),
                           html.escape(inf.get("isp", ""))))
    corps.append(tableau_lignes("Visiteurs (IPs les plus frequentes)",
                 lignes_ips, ["IP", "Hits", "Nom d'hote", "Pays", "FAI / Organisation"]))
    if st["par_jour"]:
        corps.append(tableau_lignes("Hits par jour",
                     [(j, "<span class=\"num\">%d</span>" % n)
                      for j, n in sorted(st["par_jour"].items())],
                     ["Jour", "Hits"]))
    if st["par_heure"]:
        corps.append(tableau_lignes("Hits par heure",
                     [("%sh" % hh, "<span class=\"num\">%d</span>" % st["par_heure"].get(hh, 0))
                      for hh in sorted(st["par_heure"])],
                     ["Heure", "Hits"]))
    if st["statuts"]:
        page_principale = nom_fichier(nom)
        lignes_statuts = []
        for c, n in st["statuts"].most_common():
            detail = page_principale[:-5] + "-code-%s.html" % c
            lignes_statuts.append((
                "<a href=\"%s\" target=\"_blank\">%s</a>" % (detail, c),
                html.escape(legende_statut(c)),
                "<span class=\"num\">%d</span>" % n, pct(n, total)))
        corps.append(tableau_lignes("Codes de statut",
                     lignes_statuts,
                     ["Code", "Signification", "Hits", "%"]))
        corps.append("<p class=\"note\">Cliquer un code ouvre sa page "
                     "detail dans un nouvel onglet : pages et IPs qui ont "
                     "donne ce code.</p>")
    if st["sondes"]:
        corps.append(tableau_lignes("Sondes de robots detectees (par motifs)",
                     [(html.escape(p), "<span class=\"num\">%d</span>" % n)
                      for p, n in st["sondes"].most_common(top_n)],
                     ["Chemin suspect", "Hits"]))
    if dl:
        corps.append(tableau_lignes("Fichiers les plus telecharges",
                     [(html.escape(f), "<span class=\"num\">%d</span>" % n)
                      for f, n in dl["fichiers"].most_common(top_n)],
                     ["Fichier", "Telechargements"]))
    if geoip:
        pays = Counter()
        for ip, n in st["ips"].items():
            p = infos.get(ip, {}).get("country", "")
            if p:
                pays[p] += n
        if pays:
            corps.append(tableau_lignes("Visiteurs par pays",
                         [(html.escape(p), "<span class=\"num\">%d</span>" % n)
                          for p, n in pays.most_common()],
                         ["Pays", "Hits"]))
    corps.append("<p class=\"note\">Les adresses avec ?... sont comptees sans la "
                 "partie apres ?. Les jetons FREQ (10 caracteres) sont regroupes "
                 "sous /&lt;jeton&gt;. Les sondes sont detectees par motifs, "
                 "ce n'est pas une certitude. Sources de trafic et navigateurs : "
                 "absents, les logs ne les enregistrent pas.</p>")
    corps.append("<p><a href=\"index.html\">← Retour a l'index</a></p>")
    return BASE_PAGE % ("Statistiques — " + nom, CSS, "\n".join(corps),
                        time.strftime("%Y-%m-%d %H:%M:%S"), VERSION)


def page_detail_statut(srv, st, code, top_n, infos):
    """Page detail d'un code de statut : signification, pages et IPs
    qui ont donne ce code. Generee pour chaque code present dans le log
    (aucune page pour un code a 0 hit)."""
    nom = srv["nom"]
    total = st["hits"]
    n_code = st["statuts"].get(code, 0)
    corps = ["<h1>Statistiques — %s — Code %s</h1>" % (html.escape(nom), code)]
    corps.append("<p>%s</p>" % html.escape(legende_statut(code)))
    corps.append("<p>%d hit(s) avec ce code, soit %s des hits.</p>"
                 % (n_code, pct(n_code, total)))
    pages = st["par_statut_pages"].get(code, Counter())
    corps.append(tableau_lignes("Pages qui ont donne ce code",
                 [(html.escape(p), "<span class=\"num\">%d</span>" % n,
                   pct(n, n_code)) for p, n in pages.most_common(top_n)],
                 ["Page", "Hits", "% du code"]))
    ips = st["par_statut_ips"].get(code, Counter())
    lignes_ips = []
    for ip, n in ips.most_common(top_n):
        inf = infos.get(ip, {})
        lignes_ips.append((html.escape(ip), "<span class=\"num\">%d</span>" % n,
                           html.escape(inf.get("host", "")),
                           html.escape(inf.get("country", "")),
                           html.escape(inf.get("isp", ""))))
    corps.append(tableau_lignes("IPs qui ont donne ce code",
                 lignes_ips, ["IP", "Hits", "Nom d'hote", "Pays", "FAI / Organisation"]))
    corps.append("<p><a href=\"%s\">← Retour a la page %s</a></p>"
                 % (nom_fichier(nom), html.escape(nom)))
    return BASE_PAGE % ("Statistiques — %s — Code %s" % (nom, code), CSS,
                        "\n".join(corps),
                        time.strftime("%Y-%m-%d %H:%M:%S"), VERSION)


def nom_fichier(nom):
    propre = "".join(c if (c.isalnum() or c in "-_") else "-" for c in nom)
    return "stats-" + (propre or "serveur") + ".html"


def main():
    cfg = lire_config(CONFIG_FILE)
    if cfg is None:
        return
    print("Stats v%s demarre." % VERSION)
    sortie = cfg.get("DOSSIER_SORTIE", "") or os.path.join(SCRIPT_DIR, "stats-html")
    try:
        top_n = int(cfg.get("TOP_N", "30"))
    except ValueError:
        top_n = 30
    geoip = cfg.get("GEOIP", "non").strip().lower() in ("oui", "yes", "true", "1")
    try:
        os.makedirs(sortie, exist_ok=True)
    except OSError as exc:
        print("Impossible de creer %s : %s" % (sortie, exc))
        return
    serveurs = liste_serveurs(cfg)
    if not serveurs:
        print("Aucun serveur configure : remplis les SERVEUR_N_LOG dans stats-config.conf.")
        return
    resume = []
    tous_st = []
    for srv in serveurs:
        if not srv["log"] or not os.path.isfile(srv["log"]):
            print("Ignore : %s (log introuvable : %s)" % (srv["nom"], srv["log"]))
            continue
        st = analyser_log(srv["log"])
        dl = analyser_csv(srv["csv"]) if srv["csv"] and os.path.isfile(srv["csv"]) else None
        infos = enrichir_ips(st["ips"].most_common(50), geoip)
        page = page_serveur(srv, st, dl, infos, top_n, geoip)
        chemin_page = os.path.join(sortie, nom_fichier(srv["nom"]))
        with open(chemin_page, "w", encoding="utf-8") as f:
            f.write(page)
        # Une page detail par code present (v1.4) : les anciennes
        # pages detail d'un code disparu restent dans le dossier,
        # le script ne supprime rien, comme pour les autres pages.
        for code in st["statuts"]:
            detail = chemin_page[:-5] + "-code-%s.html" % code
            with open(detail, "w", encoding="utf-8") as f:
                f.write(page_detail_statut(srv, st, code, top_n, infos))
        print("%s : %d hits, %d IPs, %d pages — %s"
              % (srv["nom"], st["hits"], len(st["ips"]), len(st["pages"]), chemin_page))
        resume.append((srv["nom"], nom_fichier(srv["nom"]), st["hits"],
                       len(st["ips"]), len(st["pages"])))
        tous_st.append(st)
    if not resume:
        print("Aucune page generee.")
        return
    lignes = ["<h1>Statistiques des serveurs</h1>"]
    # Total general : hits et sondes s'additionnent ; IPs et pages
    # sont comptees une seule fois meme si elles touchent plusieurs
    # serveurs (union, pas de double comptage).
    total_hits = sum(st["hits"] for st in tous_st)
    total_sondes = sum(st["nb_sondes"] for st in tous_st)
    ips_uniques = set()
    pages_uniques = set()
    for st in tous_st:
        ips_uniques |= set(st["ips"])
        pages_uniques |= set(st["pages"])
    lignes.append("<h2>Total general (tous serveurs)</h2>")
    lignes.append("<div class=\"cartes\"><div class=\"carte\"><b>%d</b>Hits</div>"
                  "<div class=\"carte\"><b>%d</b>IPs uniques</div>"
                  "<div class=\"carte\"><b>%d</b>Pages uniques</div>"
                  "<div class=\"carte\"><b>%d</b>Sondes detectees</div></div>"
                  % (total_hits, len(ips_uniques), len(pages_uniques), total_sondes))
    lignes.append("<p class=\"note\">IPs et pages comptees une seule fois, "
                  "meme si elles touchent plusieurs serveurs.</p>")
    lignes.append("<table><tr><th>Serveur</th><th>Hits</th><th>IPs</th><th>Pages</th></tr>")
    for nom, fich, hits, ips, pages in resume:
        lignes.append("<tr><td><a href=\"%s\">%s</a></td>"
                      "<td class=\"num\">%d</td><td class=\"num\">%d</td>"
                      "<td class=\"num\">%d</td></tr>"
                      % (fich, html.escape(nom), hits, ips, pages))
    lignes.append("</table>")
    index = BASE_PAGE % ("Statistiques des serveurs", CSS, "\n".join(lignes),
                         time.strftime("%Y-%m-%d %H:%M:%S"), VERSION)
    with open(os.path.join(sortie, "index.html"), "w", encoding="utf-8") as f:
        f.write(index)
    print("Termine : %d pages dans %s (index.html)." % (len(resume), sortie))


if __name__ == "__main__":
    main()
