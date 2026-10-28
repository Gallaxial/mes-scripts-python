# ==================== Fetcher ====================
# Telecharge automatiquement des documents gratuits (PDF, EPUB, ...)
# a partir de RECHERCHES : on donne des mots-cles, il telecharge
# TOUS les resultats (sauf limite configuree). Projet independant :
# rien a voir avec le FREQ, il ne touche ni a D:\Fichiers ni au mailbot.
#
# Sources v1.5 :
#   ia         : recherche Internet Archive (livres, textes, PDF/EPUB)
#   arxiv      : recherche arXiv (articles scientifiques, PDF)
#   gutenberg  : recherche Project Gutenberg via Gutendex (livres, EPUB)
#   s2         : recherche Semantic Scholar (articles, PDF libre acces)
#   openalex   : recherche OpenAlex (travaux, PDF libre acces)
#   ntrs       : recherche NASA NTRS (rapports techniques, PDF)
#   liste      : liens directs colles dans le config (n'importe quel site)
#
# Sujets : SUJETS (separes par ;) ou SUJET seul, sinon la RECHERCHE
# de chaque source. Avec des sujets, les fichiers vont dans
# sortie/<sujet>/<source> ; sans sujet, dans sortie/<source>.
# Progression : fetcher-etat.json retient, pour chaque paire
# source+sujet, le nombre de resultats deja examines ; la passe
# suivante reprend la. arXiv ne progresse pas (trie par date :
# il donne toujours les plus recents).
#
# v1.0 : premier jet (recherches + liste, anti-doublons, journal).
# v1.1 : SUJET central (remplace RECHERCHE quand il est rempli),
#   pas de sujet = source ignoree (plus de recherche vide),
#   comptes affiches avant telechargement, timeout API 60 s,
#   erreurs Internet Archive plus claires.
# v1.8 : le contexte SSL charge aussi des certificats racines
#   supplementaires (CERTIFICATS_SUPPLEMENTAIRES dans le config,
#   ex : racine-sslcom.pem) : certaines racines recentes (SSL.com
#   TLS Root CA 2022) sont absentes des magasins Windows alors
#   que les navigateurs les trustent via leur propre liste.
# v1.17 : suppression complete d'une source, a la demande de
#   demande 2026-10-27. Il reste 7 sources.
VERSION = "1.17"

import base64
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ---------- Configuration par defaut (surchagee par fetcher-config.conf) ----------
DOSSIER_SORTIE = "telechargements"
SUJET = ""              # le sujet pointe : remplace RECHERCHE quand il est rempli
SUJETS = ""             # plusieurs sujets separes par ; (prioritaire sur SUJET)
LIMITE_PAR_SOURCE = 0   # 0 = illimite : on telecharge TOUS les resultats
DELAI_SECONDES = 1      # pause entre deux telechargements (politesse API)

# Bases des API (constantes du script, pas du config).
IA_API = "https://archive.org"
ARXIV_API = "http://export.arxiv.org"
GUTENDEX_API = "https://gutendex.com"
S2_API = "https://api.semanticscholar.org"
OA_API = "https://api.openalex.org"
NTRS_API = "https://ntrs.nasa.gov/api"
NTRS_HOST = "https://ntrs.nasa.gov"
GUTENBERG_FICHIER = "https://www.gutenberg.org/ebooks/%d.epub.images"

CONFIG = "fetcher-config.conf"
LOG_DIR = "log"
LOG_FICHIER = os.path.join(LOG_DIR, "fetcher.log")

# Formats Internet Archive par nom de config, dans l'ordre de preference.
FORMATS_IA = {"pdf": "Text PDF", "epub": "EPUB", "txt": "DjVuTXT",
              "mobi": "MOBI", "cbz": "Comic Book Archive"}
# Gutendex donne les formats MIME de chaque livre : on choisit
# dedans selon EXTENSIONS. (IA n'offre ni AZW ni CBR : ses BD
# sont en CBZ et son Kindle en MOBI.)
FORMATS_GUTENBERG = {"application/epub+zip": "epub",
                     "application/x-mobipocket-ebook": "mobi",
                     "application/pdf": "pdf",
                     "text/html": "html", "text/plain": "txt"}


def log(msg):
    """Ecrit dans la console et dans le journal."""
    ligne = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(ligne, flush=True)
    try:
        with open(LOG_FICHIER, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except OSError:
        pass


def nom_sain(nom):
    """Nettoie un nom de fichier : garde le corps, preserve l'extension."""
    nom = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", nom).strip().strip(".")
    if not nom:
        nom = "fichier"
    if len(nom) > 120:
        corps, ext = os.path.splitext(nom)
        nom = corps[:120 - len(ext)] + ext
    return nom


_CONTEXTE_SSL = None
# Fichiers PEM de racines supplementaires, fixes par main() depuis
# le config (CERTIFICATS_SUPPLEMENTAIRES). Vides par defaut.
CERTS_SUPPLEMENTAIRES = []
DOSSIER_SCRIPT = os.path.dirname(os.path.abspath(__file__))


def contexte_ssl():
    """Contexte SSL unique pour toutes les requetes du fetcher.
    Defaut de Python, PLUS, sur Windows, les racines du magasin de
    certificats systeme (CA + ROOT) : le navigateur leur fait
    confiance, Python seul ne les voit pas toujours, et c'est ca qui
    faisait echouer gutenberg.org (self-signed dans la chaine) alors
    qu'un navigateur ouvrait le site sans probleme. Si le chargement echoue,
    on retombe sur le contexte par defaut (comportement actuel)."""
    global _CONTEXTE_SSL
    if _CONTEXTE_SSL is None:
        ctx = ssl.create_default_context()
        if sys.platform == "win32":
            try:
                pems = []
                for magasin in ("CA", "ROOT"):
                    for der, enc, _confiance in ssl.enum_certificates(magasin):
                        if enc == "x509_asn":
                            b64 = base64.b64encode(der).decode("ascii")
                            pems.append("-----BEGIN CERTIFICATE-----\n"
                                        + "\n".join(
                                            b64[i:i + 64]
                                            for i in range(0, len(b64), 64))
                                        + "\n-----END CERTIFICATE-----")
                if pems:
                    ctx.load_verify_locations(cadata="\n".join(pems))
            except Exception:
                pass
        # Racines supplementaires du config (ex : une racine recente
        # absente des magasins Windows, comme SSL.com TLS RSA Root
        # CA 2022 pour gutenberg.org). Chemins relatifs au dossier
        # du script. Fichier manquant ou illisible = avertissement,
        # pas de plantage : on continue avec le reste.
        for nom_fichier in CERTS_SUPPLEMENTAIRES:
            chemin = nom_fichier
            if not os.path.isabs(chemin):
                chemin = os.path.join(DOSSIER_SCRIPT, nom_fichier)
            try:
                ctx.load_verify_locations(cafile=chemin)
            except Exception as e:
                log("  ! certificat supplementaire %s : %s"
                    % (nom_fichier, e))
        _CONTEXTE_SSL = ctx
    return _CONTEXTE_SSL


def http_get(url, timeout=60):
    """GET simple avec un User-Agent identifiable. Retourne les octets."""
    entetes = {"User-Agent": "Fetcher/1.0"}
    req = urllib.request.Request(url, headers=entetes)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=contexte_ssl()) as rep:
        return rep.read()


def http_post_json(url, objet, timeout=60):
    """POST JSON avec un User-Agent identifiable. Retourne les octets."""
    corps = json.dumps(objet).encode("utf-8")
    req = urllib.request.Request(url, data=corps, headers={
        "User-Agent": "Fetcher/1.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=contexte_ssl()) as rep:
        return rep.read()


def telecharger(url, dest):
    """Telecharge en streaming vers dest (ecriture atomique .tmp + renommage)."""
    entetes = {"User-Agent": "Fetcher/1.0"}
    req = urllib.request.Request(url, headers=entetes)
    tmp = dest + ".tmp"
    with urllib.request.urlopen(req, timeout=60,
                                context=contexte_ssl()) as rep, \
            open(tmp, "wb") as f:
        shutil.copyfileobj(rep, f, length=1024 * 1024)
    os.replace(tmp, dest)
    return os.path.getsize(dest)


def charger_config():
    """Lit fetcher-config.conf. Seules les cles connues et les sources
    SOURCE_N_* sont retenues ; les lignes # et les inconnues sont ignorees."""
    config = {"DOSSIER_SORTIE": DOSSIER_SORTIE,
              "SUJET": SUJET,
              "SUJETS": SUJETS,
              "EXTENSIONS": "pdf,epub",
              "CERTIFICATS_SUPPLEMENTAIRES": "",
              "LIMITE_PAR_SOURCE": LIMITE_PAR_SOURCE,
              "DELAI_SECONDES": DELAI_SECONDES}
    sources = {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            lignes = f.read().splitlines()
    except OSError:
        return config, []
    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, val = ligne.split("=", 1)
        cle = cle.strip()
        val = val.strip()
        m = re.match(r"^SOURCE_(\d+)_(NOM|TYPE|RECHERCHE|URLS|FORMATS|LIMITE)$", cle)
        if m:
            sources.setdefault(int(m.group(1)), {})[m.group(2)] = val
        elif cle in config:
            try:
                config[cle] = int(val)
            except ValueError:
                config[cle] = val
    liste = []
    for n in sorted(sources):
        s = sources[n]
        if s.get("NOM") and s.get("TYPE"):
            liste.append(s)
    return config, liste


# ---------- Sources : recherches ----------

def texte_limite(limite):
    """Ce qu'on prend vraiment, a coller apres un compte de resultats."""
    return " (limite %d)" % limite if limite else " (sans limite)"


def fin_progression(debut, consommes, total, recherche):
    """Offset de la prochaine passe : debut + resultats examines.
    Si la liste est epuisee (ou si l'offset depasse un total qui a
    rapetisse), on recommence au debut."""
    fin = debut + consommes
    if total and fin >= total:
        log("  ... liste epuisee pour %r (%d resultat(s)), "
            "on recommence au debut" % (recherche, total))
        return 0
    return fin


def recherche_ia(recherche, formats, limite, debut=0):
    """Internet Archive : pagine advancedsearch, puis choisit, pour chaque
    document, le premier format disponible dans l'ordre demande.
    Ne garde que les textes (mediatype:texts) : sinon une recherche
    large ramene des collections ou des films sans fichier telechargeable.
    debut = resultats deja examines (progression) : on reprend la.
    Retourne (items, offset de la prochaine passe)."""
    if "mediatype" not in recherche.lower():
        recherche = "(%s) AND mediatype:texts" % recherche
    trouve = []
    page = debut // 50
    a_sauter = debut % 50
    total = 0
    premier = True
    while True:
        url = ("%s/advancedsearch.php?q=%s&fl[]=identifier&rows=50&page=%d"
               "&output=json" % (IA_API, urllib.parse.quote(recherche), page))
        try:
            data = json.loads(http_get(url).decode("utf-8"))
        except ValueError:
            raise ValueError("reponse Internet Archive illisible "
                             "(rien recu ou blocage)")
        rep = data.get("response", {})
        if premier:
            total = rep.get("numFound", 0)
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
            premier = False
        docs = rep.get("docs", [])
        if a_sauter:
            docs = docs[a_sauter:]
            a_sauter = 0
        for d in docs:
            ident = d.get("identifier")
            if ident:
                trouve.append(ident)
        if limite and len(trouve) >= limite:
            trouve = trouve[:limite]
            break
        if not docs or debut + len(trouve) >= total:
            break
        page += 1
    items = []
    for ident in trouve:
        try:
            meta = json.loads(http_get(
                "%s/metadata/%s" % (IA_API, ident)).decode("utf-8"))
        except Exception as e:
            log("  ! metadonnees %s : %s" % (ident, e))
            continue
        fichiers = {f.get("name"): f.get("format")
                    for f in meta.get("files", [])}
        titre = (meta.get("title") or "").strip()
        for fmt in formats:
            voulu = FORMATS_IA.get(fmt)
            for nom, format_f in fichiers.items():
                if format_f == voulu:
                    ext = "." + nom.rsplit(".", 1)[-1] if "." in nom else ""
                    items.append((
                        "%s/download/%s/%s" % (
                            IA_API, ident, urllib.parse.quote(nom)),
                        (titre + ext) if titre else nom))
                    break
            else:
                continue
            break
        if limite and len(items) >= limite:
            return items[:limite], fin_progression(
                debut, len(trouve), total, recherche)
    return items, fin_progression(debut, len(trouve), total, recherche)


def recherche_arxiv(recherche, limite):
    """arXiv : pagine l'API Atom, ne garde que les liens PDF."""
    if ":" not in recherche:
        recherche = "all:" + recherche
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
    items = []
    start = 0
    while True:
        url = ("%s/api/query?search_query=%s&start=%d&max_results=50"
               "&sortBy=submittedDate&sortOrder=descending"
               % (ARXIV_API, urllib.parse.quote(recherche), start))
        root = ET.fromstring(http_get(url))
        total = root.find("opensearch:totalResults", ns)
        total = int(total.text) if total is not None else 0
        if start == 0:
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
        lot = []
        for entree in root.findall("atom:entry", ns):
            lien_pdf = None
            for lien in entree.findall("atom:link", ns):
                if lien.get("title") == "pdf":
                    lien_pdf = lien.get("href")
            ident = entree.find("atom:id", ns)
            titre = entree.find("atom:title", ns)
            if lien_pdf and ident is not None:
                base = ident.text.rstrip("/").rsplit("/", 1)[-1]
                base = re.sub(r"v\d+$", "", base)
                if titre is not None and titre.text and titre.text.strip():
                    # Le titre, pas le numero : 2610.35846.pdf ne dit rien.
                    nom = " ".join(titre.text.split()) + ".pdf"
                else:
                    nom = base + ".pdf"
                lot.append((lien_pdf, nom))
        items.extend(lot)
        if limite and len(items) >= limite:
            return items[:limite]
        start += 50
        if not lot or start >= total:
            break
    return items


def ext_dans_liste(ext, liste):
    """Vrai si l'extension est acceptee : 'azw*' accepte azw, azw3,
    azw4... ; sinon il faut l'egalite exacte."""
    ext = ext.lower()
    for motif in liste:
        if motif.endswith("*"):
            if ext.startswith(motif[:-1]):
                return True
        elif ext == motif:
            return True
    return False


def recherche_gutenberg(recherche, extensions, limite, debut=0):
    """Project Gutenberg via Gutendex : cherche, puis choisit pour
    chaque livre le premier format accepte (EXTENSIONS, dans l'ordre)
    parmi ceux que Gutendex annonce pour ce livre.
    Sans annonce de format, repli sur l'EPUB direct (ancien
    comportement), si epub est accepte.
    debut = resultats deja examines (progression) : on saute les
    premieres pages via les liens next. Retourne (items, offset)."""
    items = []
    url = "%s/books?search=%s" % (GUTENDEX_API, urllib.parse.quote(recherche))
    premier = True
    total = 0
    vus = 0
    a_sauter = debut
    while url:
        data = json.loads(http_get(url).decode("utf-8"))
        if premier:
            total = data.get("count", 0)
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
            premier = False
        for livre in data.get("results", []):
            if a_sauter:
                a_sauter -= 1
                continue
            vus += 1
            bid = livre.get("id")
            if bid:
                titre = (livre.get("title") or "").strip()
                formats_livre = {}
                for mime, url_f in (livre.get("formats") or {}).items():
                    ext = FORMATS_GUTENBERG.get(mime)
                    if ext:
                        formats_livre[ext] = url_f
                choix = None
                for motif in extensions:
                    for ext, url_f in formats_livre.items():
                        if ext_dans_liste(ext, [motif]):
                            choix = (url_f, ext)
                            break
                    if choix:
                        break
                if choix:
                    url_f, ext = choix
                    items.append((url_f, (titre + "." + ext) if titre else
                                  "%d.%s" % (bid, ext)))
                elif not formats_livre and ext_dans_liste("epub", extensions):
                    items.append((GUTENBERG_FICHIER % bid,
                                  (titre + ".epub") if titre else
                                  "%d.epub" % bid))
            if limite and len(items) >= limite:
                return items[:limite], fin_progression(
                    debut, vus, total, recherche)
        url = data.get("next")
    return items, fin_progression(debut, vus, total, recherche)


def nom_fichier_url(url, secours):
    """Nom de fichier depuis une URL, ou le nom de secours si l'URL
    ne finit pas par un nom utilisable."""
    nom = urllib.parse.unquote(
        urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if nom.lower().endswith(".pdf"):
        return nom
    return secours


def recherche_s2(recherche, limite, debut=0):
    """Semantic Scholar : ne garde que les articles avec un PDF
    en libre acces (openAccessPdf). debut = offset de depart
    (progression). Retourne (items, offset de la prochaine passe)."""
    items = []
    vus = 0
    offset = debut
    total = 0
    premier = True
    while True:
        url = ("%s/graph/v1/paper/search?query=%s&fields=title,openAccessPdf"
               "&limit=100&offset=%d"
               % (S2_API, urllib.parse.quote(recherche), offset))
        data = json.loads(http_get(url).decode("utf-8"))
        if premier:
            total = data.get("total", 0)
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
            premier = False
        lot = data.get("data", [])
        for i, papier in enumerate(lot, 1):
            oa = papier.get("openAccessPdf") or {}
            pdf = oa.get("url")
            if pdf:
                titre = (papier.get("title") or "").strip()
                items.append((pdf, (titre + ".pdf") if titre else
                              nom_fichier_url(
                                  pdf, papier.get("paperId", "article") + ".pdf")))
            if limite and len(items) >= limite:
                return items[:limite], fin_progression(
                    debut, vus + i, total, recherche)
        vus += len(lot)
        offset += 100
        if not lot or offset >= total:
            break
    return items, fin_progression(debut, vus, total, recherche)


def recherche_openalex(recherche, limite, debut=0):
    """OpenAlex : ne garde que les travaux avec un PDF en libre acces
    (best_oa_location). debut = resultats deja examines (progression) :
    on reprend a la page qui contient ce rang. Retourne (items, offset)."""
    items = []
    vus = 0
    page = debut // 100 + 1
    a_sauter = debut % 100
    total = 0
    premier = True
    while True:
        url = ("%s/works?search=%s&per_page=100&page=%d"
               % (OA_API, urllib.parse.quote(recherche), page))
        data = json.loads(http_get(url).decode("utf-8"))
        meta = data.get("meta", {})
        if premier:
            total = meta.get("count", 0)
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
            premier = False
        lot = data.get("results", [])
        if a_sauter:
            lot = lot[a_sauter:]
            a_sauter = 0
        for i, travail in enumerate(lot, 1):
            loc = travail.get("best_oa_location") or {}
            pdf = loc.get("pdf_url")
            if not pdf:
                continue
            titre = (travail.get("title") or "").strip()
            if titre:
                items.append((pdf, titre + ".pdf"))
            else:
                doi = (travail.get("doi") or "").replace("https://doi.org/", "")
                secours = (doi.replace("/", "_") if doi else
                           travail.get("id", "travail").rsplit("/", 1)[-1]) + ".pdf"
                items.append((pdf, nom_fichier_url(pdf, secours)))
            if limite and len(items) >= limite:
                return items[:limite], fin_progression(
                    debut, vus + i, total, recherche)
        vus += len(lot)
        if not lot or page * 100 >= total:
            break
        page += 1
    return items, fin_progression(debut, vus, total, recherche)


def recherche_ntrs(recherche, limite, debut=0):
    """NASA NTRS : recherche POST (q + page.size/from), ne garde que les
    documents avec un PDF (disseminated=DOCUMENT_AND_METADATA).
    La doc montre les parametres a la fois en query string et en corps
    JSON : on envoie les deux, avec les memes valeurs.
    Retourne (items, offset de la prochaine passe)."""
    items = []
    vus = 0
    offset = debut
    total = 0
    premier = True
    while True:
        params = ("?q=%s&page.size=100&page.from=%d"
                  "&disseminated=DOCUMENT_AND_METADATA"
                  % (urllib.parse.quote(recherche), offset))
        corps = {"q": recherche,
                 "page": {"size": 100, "from": offset},
                 "disseminated": "DOCUMENT_AND_METADATA"}
        data = json.loads(http_post_json(
            NTRS_API + "/citations/search" + params, corps).decode("utf-8"))
        if premier:
            total = data.get("stats", {}).get("total", 0)
            log("  ... %d resultat(s) pour %r%s"
                % (total, recherche, texte_limite(limite)))
            premier = False
        lot = data.get("results", [])
        for i, cit in enumerate(lot, 1):
            pdf_url = None
            nom_dl = None
            if cit.get("downloadsAvailable"):
                for dl in cit.get("downloads", []):
                    if dl.get("mimetype") == "application/pdf":
                        liens = dl.get("links", {})
                        chemin = liens.get("pdf") or liens.get("original")
                        if chemin:
                            # Les liens de la doc contiennent deja
                            # /api : on met juste l'hote devant, sinon
                            # ca donne /api/api/... et 404 partout.
                            pdf_url = (NTRS_HOST + chemin
                                       if chemin.startswith("/")
                                       else chemin)
                            nom_dl = dl.get("name")
                        break
            if pdf_url:
                titre = (cit.get("title") or "").strip()
                items.append((pdf_url, (titre + ".pdf") if titre else
                              (nom_dl or "nasa.pdf")))
            if limite and len(items) >= limite:
                return items[:limite], fin_progression(
                    debut, vus + i, total, recherche)
        vus += len(lot)
        offset += 100
        if not lot or offset >= total:
            break
    return items, fin_progression(debut, vus, total, recherche)


def source_liste(urls):
    """Type liste : liens directs separes par ; dans le config."""
    items = []
    for u in urls.split(";"):
        u = u.strip()
        if not u:
            continue
        nom = urllib.parse.unquote(
            urllib.parse.urlparse(u).path.rstrip("/").rsplit("/", 1)[-1])
        if nom:
            items.append((u, nom))
    return items


# ---------- Moteur ----------

# Fichier d'etat : ou chaque paire source+sujet s'est arretee.
# Relatif au dossier du script, comme le config et le journal.
# Efface-le pour recommencer toutes les listes au debut.
ETAT = "fetcher-etat.json"

# Types qui progressent : tri stable, on peut reprendre plus loin.
# arXiv ne progresse pas (trie par date : les plus recents d'abord,
# chaque passe attrape les nouveaux) ; liste n'a pas de recherche.
PROGRESSION_TYPES = ("ia", "s2", "openalex", "gutenberg", "ntrs")


def charger_etat():
    """Etat de progression, ou {} si absent ou illisible."""
    if not os.path.exists(ETAT):
        return {}
    try:
        with open(ETAT, "r", encoding="utf-8") as f:
            etat = json.load(f)
        if isinstance(etat, dict):
            return {str(k): int(v) for k, v in etat.items()}
    except (OSError, ValueError):
        pass
    log("Etat de progression illisible : on recommence au debut")
    return {}


def sauver_etat(etat):
    """Ecriture atomique de l'etat de progression."""
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, ETAT)


def limite_source(source, config):
    """Limite pour UNE source (v1.15) : SOURCE_N_LIMITE si elle
    est remplie et lisible, sinon LIMITE_PAR_SOURCE (20 par
    defaut). 0 = illimite, comme pour la limite globale."""
    brut = (source.get("LIMITE") or "").strip()
    if brut:
        try:
            return int(brut)
        except ValueError:
            pass
    return config["LIMITE_PAR_SOURCE"]


def traiter_source(source, config, sujet=None, etat=None):
    """Une source = une recherche + tous ses telechargements.
    sujet = le sujet pointe pour cette passe (None = mode ancien :
    RECHERCHE de la source, dossier sortie/<source>).
    Avec un sujet, le dossier devient sortie/<sujet>/<source>.
    etat = progression : la passe reprend ou la precedente s'est
    arretee, et l'etat est mis a jour a la fin de la passe."""
    nom = nom_sain(source["NOM"])
    type_source = source["TYPE"].strip().lower()
    if sujet:
        dossier = os.path.join(config["DOSSIER_SORTIE"], nom_sain(sujet), nom)
    else:
        dossier = os.path.join(config["DOSSIER_SORTIE"], nom)
    os.makedirs(dossier, exist_ok=True)
    for f in os.listdir(dossier):
        if f.endswith(".tmp"):
            try:
                os.remove(os.path.join(dossier, f))
            except OSError:
                pass
    limite = limite_source(source, config)
    if limite != config["LIMITE_PAR_SOURCE"]:
        log("  ... limite par source : %d (au lieu de %d)"
            % (limite, config["LIMITE_PAR_SOURCE"]))
    recherche = (sujet or "").strip() or source.get("RECHERCHE", "").strip()
    extensions = [e.strip().lower()
                  for e in config.get("EXTENSIONS", "pdf,epub").split(",")
                  if e.strip()]
    if not extensions:
        extensions = ["pdf", "epub"]
    if type_source in ("ia", "arxiv", "gutenberg", "s2", "openalex",
                       "ntrs") \
            and not recherche:
        log("  ! source %s : pas de sujet (ni SUJETS, ni SUJET, ni RECHERCHE),"
            " ignoree" % nom)
        return 0, 0, 0
    if type_source in ("arxiv", "s2", "openalex", "ntrs") \
            and not ext_dans_liste("pdf", extensions):
        log("  ! source %s : pdf pas dans EXTENSIONS, ignoree" % nom)
        return 0, 0, 0
    cle = None
    debut = 0
    if type_source in PROGRESSION_TYPES and etat is not None:
        cle = "%s|%s" % (nom, recherche)
        debut = etat.get(cle, 0)
        if debut:
            log("  ... reprise au resultat %d" % (debut + 1))
    fin = None
    if type_source == "ia":
        formats = [f.strip().lower()
                   for f in source.get("FORMATS", ",".join(extensions)).split(",")
                   if f.strip()]
        items, fin = recherche_ia(recherche, formats, limite, debut)
    elif type_source == "arxiv":
        items = recherche_arxiv(recherche, limite)
    elif type_source == "gutenberg":
        items, fin = recherche_gutenberg(recherche, extensions, limite, debut)
    elif type_source == "s2":
        items, fin = recherche_s2(recherche, limite, debut)
    elif type_source == "openalex":
        items, fin = recherche_openalex(recherche, limite, debut)
    elif type_source == "ntrs":
        items, fin = recherche_ntrs(recherche, limite, debut)
    elif type_source == "liste":
        items = source_liste(source.get("URLS", ""))
        if limite:
            items = items[:limite]
    else:
        log("  ! type inconnu %r pour la source %s, ignoree"
            % (type_source, nom))
        return 0, 0, 0
    recus, deja, erreurs = 0, 0, 0
    vus = set()
    for url, nom_fichier in items:
        dest = os.path.join(dossier, nom_sain(nom_fichier))
        base, ext = os.path.splitext(dest)
        n = 2
        while dest in vus:
            # Deux documents avec le meme titre : le 2e prend " (2)".
            dest = "%s (%d)%s" % (base, n, ext)
            n += 1
        vus.add(dest)
        if os.path.exists(dest):
            deja += 1
            continue
        try:
            taille = telecharger(url, dest)
            recus += 1
            log("  + %s (%d Ko)" % (os.path.basename(dest), taille // 1024))
        except Exception as e:
            erreurs += 1
            log("  ! %s : %s" % (os.path.basename(dest), e))
        if config["DELAI_SECONDES"]:
            time.sleep(config["DELAI_SECONDES"])
    log("  -> %s : %d recu(s), %d deja la, %d erreur(s)"
        % (nom, recus, deja, erreurs))
    if cle is not None and fin is not None:
        etat[cle] = fin
    return recus, deja, erreurs


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    log("Fetcher v%s actif" % VERSION)
    config, sources = charger_config()
    global CERTS_SUPPLEMENTAIRES
    CERTS_SUPPLEMENTAIRES = [
        f.strip()
        for f in config.get("CERTIFICATS_SUPPLEMENTAIRES", "").split(",")
        if f.strip()]
    if not sources:
        log("Aucune source active dans %s : ajoute une source (voir LISEZMOI)"
            % CONFIG)
        return 0
    os.makedirs(config["DOSSIER_SORTIE"], exist_ok=True)
    # Sujets : SUJETS (separes par ;) d'abord, sinon SUJET seul,
    # sinon chaque source reprend sa RECHERCHE (liste vide ici).
    sujets = [s.strip() for s in config.get("SUJETS", "").split(";")
              if s.strip()]
    if not sujets and config.get("SUJET", "").strip():
        sujets = [config["SUJET"].strip()]
    etat = charger_etat()
    tot_recus = tot_deja = tot_erreurs = 0
    for source in sources:
        type_source = source["TYPE"].strip().lower()
        # Une liste d'URLs ne depend d'aucun sujet : une seule passe.
        passes = [None] if type_source == "liste" or not sujets else sujets
        for sujet in passes:
            if sujet:
                log("Source %s (%s) — sujet %r"
                    % (source["NOM"], source["TYPE"], sujet))
            else:
                log("Source %s (%s)" % (source["NOM"], source["TYPE"]))
            try:
                r, d, e = traiter_source(source, config, sujet, etat)
                tot_recus += r
                tot_deja += d
                tot_erreurs += e
            except Exception as e:
                log("  ! source %s en erreur : %s" % (source["NOM"], e))
            sauver_etat(etat)
    log("Termine : %d recu(s), %d deja la, %d erreur(s)"
        % (tot_recus, tot_deja, tot_erreurs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
