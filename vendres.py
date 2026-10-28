# vendres.py — Vendres, petites annonces, en Python (bibliotheque standard seulement).
#
# Portage Python de l'ancien site PHP (index.php + contact-send.php + track.php
# + stats.php). Meme comportement, meme design sombre, aucune dependance :
# pas d'Apache, pas de PHP, pas de module externe. ffmpeg (si present) sert
# juste a generer les miniatures, comme avant ; sans lui, les photos
# originales sont servies.
#
# VERSION = "3.6"  (regle MAJEUR.MINEUR : +1 majeur = nouvelle fonction ou
#   v3.6 : date devant chaque visite dans vendres.log + purge du journal
#   au demarrage (PURGE_JOURS, defaut 30 jours).
# changement de comportement, +1 mineur = correctif ou texte)
#
# Fichiers de configuration / donnees (a cote du script, jamais ecrasés
# par une mise a jour du script) :
#   vendres-config.json  -> port, dossier des annonces, ffmpeg, admin
#   mail-settings.json   -> SMTP (copier le fichier existant ; ne JAMAIS
#                           mettre de mot de passe dans le code)
#   stats-data.json      -> vues/lectures (copier l'ancien pour garder l'historique)
#   contact-rl.json      -> limite de messages (5/heure/IP)
#
# Lancement : lance-vendres.bat (ou : python vendres.py)
# Page d'accueil : http://exemple.com:8082/  (port configurable)

import base64
import hashlib
import hmac
import html
import io
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import threading
import time
import urllib.parse
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "3.6"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "vendres-config.json")
STATS_FILE = os.path.join(SCRIPT_DIR, "stats-data.json")
RL_FILE = os.path.join(SCRIPT_DIR, "contact-rl.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass
LOG_FILE = os.path.join(LOG_DIR, "vendres.log")

DEFAULTS = {
    "PORT": 8082,
    "HOST": "0.0.0.0",
    "ITEMS_DIR": "D:/Vendres/items",
    "FFMPEG": "D:/Vendres/ffmpeg.exe",
    "ITEMS_PER_PAGE": 20,
    "ADMIN_USER": "admin",
    "ADMIN_PASS": "",
    "MAIL_SETTINGS": "mail-settings.json",
    "CONTACT_TO": "vous@exemple.com",
    "PURGE_JOURS": 30,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cfg:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Config illisible (%s), defauts utilises." % e)
    cfg["PORT"] = int(cfg["PORT"])
    cfg["ITEMS_PER_PAGE"] = int(cfg["ITEMS_PER_PAGE"])
    return cfg


CONFIG = load_config()
FILE_LOCK = threading.Lock()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
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


# ------------------------------------------------------------------ utilitaires
def h(s):
    return html.escape("" if s is None else str(s), quote=True)


def first_line(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
    except OSError:
        return None
    if line == "":
        return None
    return line.strip()


def natkey(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", s)]


def parse_price(folder):
    raw = first_line(os.path.join(folder, "price.txt"))
    if raw is None or raw == "":
        return None
    if len(raw) > 10:
        return {"ok": False}
    if not re.match(r"^[\d$.,\s]*\d[\d$.,\s]*$", raw):
        return {"ok": False}
    text = raw
    if "$" not in text and "\u20ac" not in text:
        text = "$" + text
    return {"ok": True, "text": text}


def parse_contact(folder):
    email = first_line(os.path.join(folder, "contact.txt"))
    if email is None or email == "":
        return None
    if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
        return None
    return email


def item_images(folder):
    d = os.path.join(folder, "images")
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for f in names:
        if f.startswith("."):
            continue
        if re.search(r"\.(jpe?g|png|webp|gif)$", f, re.I):
            out.append(f)
    out.sort(key=natkey)
    return out


def cover_image(images):
    for f in images:
        if re.match(r"^0\.(jpe?g|png|webp|gif)$", f, re.I):
            return f
    return images[0] if images else None


def scan_items():
    items = []
    base = CONFIG["ITEMS_DIR"]
    try:
        names = os.listdir(base)
    except OSError:
        return items
    for n in names:
        if n in (".", "..") or n.startswith("_") or n.startswith("."):
            continue
        full = os.path.join(base, n)
        if not os.path.isdir(full):
            continue
        try:
            with open(os.path.join(full, "description.txt"), "r",
                      encoding="utf-8", errors="replace") as f:
                desc = f.read().strip()
        except OSError:
            desc = ""
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            mtime = 0
        items.append({
            "name": n, "mtime": mtime, "desc": desc,
            "price": parse_price(full), "email": parse_contact(full),
            "images": item_images(full),
        })
    items.sort(key=lambda it: it["mtime"], reverse=True)
    return items



def short_desc(d, length=160):
    d = (d or "").strip()
    if d == "":
        return "Aucune description."
    if len(d) <= length:
        return d
    return d[:length] + "\u2026"


# ------------------------------------------------------------------ images
IMG_EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


def resolve_item_file(rel):
    rel = rel.replace("\x00", "").replace("\\", "/")
    rel = re.sub(r"/+", "/", rel).strip("/")
    rel = rel.replace("../", "")
    base = os.path.realpath(CONFIG["ITEMS_DIR"])
    path = os.path.realpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep) and path != base:
        return False
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1][1:].lower()
    if ext not in IMG_EXTS:
        return False
    return path


def sized_image(src, w, hgt, crop=True):
    tdir = os.path.join(CONFIG["ITEMS_DIR"], "_thumbs")
    try:
        os.makedirs(tdir, exist_ok=True)
    except OSError:
        return False
    key = ("" if crop else "fit|") + src + str(w) + "x" + str(hgt)
    t = os.path.join(tdir, hashlib.md5(key.encode("utf-8")).hexdigest() + ".jpg")
    try:
        if os.path.isfile(t) and os.path.getmtime(t) >= os.path.getmtime(src):
            return t
    except OSError:
        pass
    ff = CONFIG["FFMPEG"]
    if not ff or not os.path.isfile(ff):
        return False
    if crop:
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d" % (w, hgt, w, hgt))
    else:
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease" % (w, hgt))
    try:
        rc = subprocess.run([ff, "-y", "-v", "error", "-i", src,
                             "-vf", vf, "-q:v", "4", t],
                            capture_output=True, timeout=60).returncode
    except Exception:
        return False
    return t if (rc == 0 and os.path.isfile(t)) else False


# ------------------------------------------------------------------ admin
def vad_cookie_val(exp):
    msg = (str(exp) + "|vendres-admin").encode("utf-8")
    sig = hmac.new(str(CONFIG["ADMIN_PASS"]).encode("utf-8"), msg,
                   hashlib.sha256).hexdigest()
    return str(exp) + "." + sig


def is_admin(cookies):
    if not CONFIG["ADMIN_PASS"]:
        return False
    c = cookies.get("vadm")
    if not c or "." not in c:
        return False
    exp, _sig = c.split(".", 1)
    if not exp.isdigit() or int(exp) < int(time.time()):
        return False
    return hmac.compare_digest(vad_cookie_val(exp), c)


def vad_csrf(cookie_val):
    msg = ("del|" + (cookie_val or "")).encode("utf-8")
    return hmac.new(str(CONFIG["ADMIN_PASS"]).encode("utf-8"), msg,
                    hashlib.sha256).hexdigest()


def rrmdir(d):
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isdir(p):
            rrmdir(p)
        else:
            try:
                os.unlink(p)
            except OSError:
                pass
    try:
        os.rmdir(d)
    except OSError:
        pass


# ------------------------------------------------------------------ pages HTML
CSS_MAIN = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#070b16;color:#e8ecf4;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;position:relative}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.55;background:
 radial-gradient(1px 1px at 12% 22%,#fff 100%,transparent 100%),
 radial-gradient(1px 1px at 68% 8%,#fff 100%,transparent 100%),
 radial-gradient(1.5px 1px at 34% 66%,#cfe4ff 100%,transparent 100%),
 radial-gradient(1px 1px at 82% 44%,#fff 100%,transparent 100%),
 radial-gradient(2px 2px at 52% 88%,#cfe4ff 100%,transparent 100%),
 radial-gradient(1200px 600px at 50% -10%,rgba(90,60,180,.25),transparent 70%)}
.wrap{max-width:1100px;margin:0 auto;padding:26px 18px 40px;position:relative;z-index:1}
header{text-align:center;margin-bottom:22px}
.revwrap{text-align:center;margin-top:10px}
.revbtn{display:inline-block;padding:8px 20px;border-radius:10px;background:#0d0d0d;color:#555;text-decoration:none;font-size:.85rem;border:1px solid #2a2a2a}
.revbtn.hot{background:linear-gradient(135deg,#f59e0b,#ef4444);color:#fff;font-weight:700;border:none;box-shadow:0 0 14px rgba(245,158,11,.65);animation:pulse 1.6s infinite}
.card{position:relative}
.admindel{position:absolute;top:6px;right:6px;z-index:5;background:rgba(10,10,10,.7);border:1px solid #f87171;border-radius:50%;width:34px;height:34px;cursor:pointer;font-size:1.05rem;line-height:1;padding:0}
.admindel:hover{background:#f87171}
@keyframes pulse{0%,100%{box-shadow:0 0 8px rgba(245,158,11,.4)}50%{box-shadow:0 0 18px rgba(245,158,11,.9)}}
h1{font-size:2rem;letter-spacing:.18em;color:#fff;text-shadow:0 0 26px rgba(150,120,255,.8)}
.sub{color:#9aa8c3;margin-top:6px;font-size:.95rem}
.grid{display:flex;flex-direction:column;gap:14px}
body.cols2 .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
body.cols2 .card{flex-direction:column}
body.cols2 .cover{width:100%;flex:none;height:170px}
@media (max-width:700px){body.cols2 .grid{grid-template-columns:1fr}}
.card{display:flex;background:rgba(20,28,50,.92);border:1px solid rgba(77,166,255,.28);border-radius:14px;overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s;text-align:left}
.card:hover{transform:translateY(-2px);border-color:rgba(125,211,252,.7)}
.cover{width:300px;height:180px;flex:0 0 300px;background:#0d1428;display:flex;align-items:center;justify-content:center;overflow:hidden}
.cover img{width:100%;height:100%;object-fit:contain}
.cover .none{font-size:52px;opacity:.4}
.cbody{flex:1;padding:14px 18px;min-width:0}
.ctitle{font-size:1.12rem;font-weight:700;color:#fff;margin-bottom:6px;word-break:break-word}
.cprice{display:inline-block;background:linear-gradient(135deg,#1e9e5a,#37c871);color:#fff;font-weight:700;border-radius:8px;padding:4px 12px;margin-bottom:8px;font-size:1.02rem}
.cprice.na{background:rgba(120,130,150,.25);color:#aab4cc;font-weight:600}
.cdesc{color:#9aa8c3;font-size:.92rem;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.more{color:#7dd3fc;font-size:.88rem;margin-top:6px;display:inline-block}
.empty{text-align:center;color:#9aa8c3;font-size:1.05rem;padding:60px 20px;line-height:1.8}
#ov{position:fixed;inset:0;z-index:50;background:rgba(2,4,10,.88);display:none;align-items:center;justify-content:center;padding:18px}
#ov.open{display:flex}
#ovbox{background:#101a33;border:1px solid rgba(77,166,255,.35);border-radius:16px;max-width:860px;width:100%;max-height:92vh;overflow-y:auto;padding:22px;position:relative}
#ovclose{position:absolute;top:12px;right:14px;background:rgba(77,166,255,.12);border:1px solid rgba(77,166,255,.35);color:#e8ecf4;font-size:18px;width:36px;height:36px;border-radius:50%;cursor:pointer}
#ovtitle{font-size:1.4rem;color:#fff;margin-bottom:4px;padding-right:44px;word-break:break-word}
#ovprice{display:inline-block;background:linear-gradient(135deg,#1e9e5a,#37c871);color:#fff;font-weight:700;border-radius:8px;padding:5px 14px;margin:8px 0;font-size:1.1rem}
#ovprice.na{background:rgba(120,130,150,.25);color:#aab4cc}
#ovmain{width:auto;height:auto;max-width:100%;max-height:70vh;margin:10px auto;background:#0d1428;border-radius:10px;display:none}
#ovthumbs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
#ovthumbs img{width:74px;height:56px;object-fit:cover;border-radius:8px;cursor:pointer;border:2px solid transparent}
#ovthumbs img.sel{border-color:#7dd3fc}
#ovdesc{color:#c6d2e8;line-height:1.65;white-space:pre-wrap;word-break:break-word;margin:10px 0}
#ovcontact{display:inline-block;margin-top:8px;background:linear-gradient(135deg,#1c5fb8,#2f86e8);color:#fff;text-decoration:none;font-weight:700;padding:12px 22px;border-radius:10px}
.pages{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:26px 0 8px}
.pages a,.pages .cur{padding:9px 15px;border-radius:10px;border:1px solid rgba(77,166,255,.35);color:#e8ecf4;text-decoration:none;background:rgba(20,28,50,.9)}
.pages a:hover{border-color:#7dd3fc}
.pages .cur{background:linear-gradient(135deg,#1c5fb8,#2f86e8);font-weight:700;border-color:transparent}
.pages .dots{color:#5a6a8a;padding:9px 2px}
.count{text-align:center;color:#5a6a8a;font-size:.85rem}
footer{text-align:center;color:#5a6a8a;font-size:.8rem;margin-top:34px}
footer a{color:#7dd3fc;text-decoration:none}
footer a:hover{text-decoration:underline}
@media (max-width:480px){
 .wrap{padding:18px 12px 30px}
 h1{font-size:1.5rem}
 .card{flex-direction:column}
 .cover{width:100%;flex:none;height:210px}
 .ctitle{font-size:1.25rem}
 #ovbox{padding:16px}
}
"""

def render_contact_modal(lang):
    html = """
<div id="contactModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;align-items:center;justify-content:center">
<div style="background:#141428;border:1px solid #3fa9f5;border-radius:14px;padding:24px;width:min(92vw,440px);box-sizing:border-box">
<h2 style="color:#7dd3fc;margin:0 0 4px;font-size:1.2rem">&#x2709;&#xFE0F; Contact Us</h2>
<p style="color:#9aa;font-size:.85rem;margin:0 0 12px">Envoyez un message &agrave; Luc.</p>
<div id="cErr" style="display:none;color:#f87171;font-size:.85rem;margin-bottom:8px"></div>
<div id="cOk" style="display:none;color:#7dff9a;font-size:.9rem;margin-bottom:8px">Message envoy&eacute;!</div>
<form id="contactForm">
<input name="name" placeholder="Votre nom" required maxlength="60" style="width:100%;box-sizing:border-box;margin:6px 0;padding:10px;border-radius:8px;border:1px solid #444;background:#0b0b18;color:#fff">
<input name="email" type="email" placeholder="Votre email" required maxlength="120" style="width:100%;box-sizing:border-box;margin:6px 0;padding:10px;border-radius:8px;border:1px solid #444;background:#0b0b18;color:#fff">
<textarea name="message" id="cMsg" placeholder="Votre message" required maxlength="5000" rows="5" style="width:100%;box-sizing:border-box;margin:6px 0;padding:10px;border-radius:8px;border:1px solid #444;background:#0b0b18;color:#fff;resize:vertical"></textarea>
<div style="display:flex;justify-content:space-between;align-items:center;margin:2px 0 6px">
<span id="cCount" style="color:#9aa;font-size:.75rem">0 / 5000</span>
<label style="color:#7dd3fc;font-size:.8rem;cursor:pointer">&#x1F4CE; Joindre des fichiers (max 5 Mo total)
<input type="file" name="attach[]" id="cFile" multiple style="display:none"></label>
</div>
<div id="cFileName" style="color:#9aa;font-size:.75rem;margin-bottom:4px"></div>
<div style="display:flex;gap:10px;margin-top:8px">
<button type="submit" style="flex:1;background:#3fa9f5;color:#04121f;font-weight:700;padding:11px;border:none;border-radius:8px;cursor:pointer;font-size:1rem">Envoyer</button>
<button type="button" id="contactClose" style="background:#2a2a4a;color:#fff;padding:11px 18px;border:none;border-radius:8px;cursor:pointer">✕</button>
</div>
</form>
</div>
</div>
<script>
(function(){
var m=document.getElementById('contactModal'),b=document.getElementById('contactBtn'),c=document.getElementById('contactClose');
if(!m||!b) return;
b.onclick=function(){ m.style.display='flex'; document.getElementById('cErr').style.display='none'; document.getElementById('cOk').style.display='none'; };
c.onclick=function(){ m.style.display='none'; };
m.onclick=function(e){ if(e.target===m) m.style.display='none'; };
var cm=document.getElementById('cMsg'),cc=document.getElementById('cCount');
if(cm&&cc){ cm.addEventListener('input',function(){ cc.textContent=cm.value.length+' / 5000'; }); }
var cf=document.getElementById('cFile'),cfn=document.getElementById('cFileName');
if(cf&&cfn){ cf.addEventListener('change',function(){ cfn.textContent=cf.files.length?(cf.files.length+' fichier(s): '+Array.from(cf.files).map(function(f){return f.name;}).join(', ')):''; }); }
document.getElementById('contactForm').onsubmit=function(e){
  e.preventDefault();
  var fd=new FormData(this), err=document.getElementById('cErr'), ok=document.getElementById('cOk');
  err.style.display='none'; ok.style.display='none';
  fetch('/contact-send',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
    if(d.ok){ ok.style.display='block'; document.getElementById('contactForm').reset(); setTimeout(function(){m.style.display='none';},1800); }
    else { err.textContent=d.err||'Erreur.'; err.style.display='block'; }
  }).catch(function(){ err.textContent='Erreur r\u00e9seau.'; err.style.display='block'; });
};
})();
</script>
"""
    html = html.replace("&#x2709;&#xFE0F; Contact Us", tr(lang, "cm_title"))
    html = html.replace("Envoyez un message &agrave; Luc.", tr(lang, "cm_sub"))
    html = html.replace("Message envoy&eacute;!", tr(lang, "cm_sent"))
    html = html.replace('placeholder="Votre nom"', 'placeholder="' + tr(lang, "cm_name") + '"')
    html = html.replace('placeholder="Votre email"', 'placeholder="' + tr(lang, "cm_email") + '"')
    html = html.replace('placeholder="Votre message"', 'placeholder="' + tr(lang, "cm_msg") + '"')
    html = html.replace("&#x1F4CE; Joindre des fichiers (max 5 Mo total)", tr(lang, "cm_attach"))
    html = html.replace(">Envoyer</button>", ">" + tr(lang, "cm_send") + "</button>")
    html = html.replace("d.err||'Erreur.'", "d.err||TR.cm_err")
    html = html.replace("err.textContent='Erreur réseau.'", "err.textContent=TR.cm_err_net")
    html = html.replace("(cf.files.length+' fichier(s): '+Array.from(cf.files).map(function(f){return f.name;}).join(', '))",
                        "(TR.cm_files.replace('{n}',cf.files.length).replace('{names}',Array.from(cf.files).map(function(f){return f.name;}).join(', ')))")
    return html


def render_listing(page_items, page, pages, total, is_adm, pend, csrf, js_items, lang, toggle):
    out = []
    out.append("<!DOCTYPE html>\n<html lang=\"" + lang + "\">\n<head>\n<meta charset=\"UTF-8\">\n"
               "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1.0\">\n"
               "<title>" + tr(lang, "title") + "</title>\n<style>")
    out.append(CSS_MAIN)
    out.append("</style>\n</head>\n<body>\n<div class=\"wrap\">\n<header>\n<h1>SELL / Vendres</h1>\n"
               "<div class=\"sub\">" + tr(lang, "sub") + "</div>\n"
               "<div style=\"display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:10px\">\n")
    out.append(lang_btn(toggle, lang))
    out.append("<div class=\"revwrap\" style=\"margin-top:0\">")
    if is_adm:
        out.append("<a class=\"revbtn hot\" href=\"/?admin=logout\" title=\"" + tr(lang, "admin_logout_title") + "\">\U0001f513 Admin \u2713</a>")
    else:
        out.append("<a class=\"revbtn\" href=\"/?admin=login\" title=\"" + tr(lang, "admin_title") + "\">\U0001f527 Admin</a>")
    out.append("</div>\n"
               "<div class=\"revwrap\" style=\"margin-top:0\"><a class=\"revbtn\" href=\"/stats\" "
               "title=\"" + tr(lang, "stats_title") + "\">" + tr(lang, "stats_btn") + "</a></div>\n"
               "<div class=\"revwrap\" style=\"margin-top:0\"><button class=\"revbtn\" id=\"contactBtn\" "
               "title=\"" + tr(lang, "contact_title") + "\" style=\"background:#1a1a2e;border-color:#ffd93d;color:#ffd93d\">"
               + tr(lang, "contact_btn") + "</button></div>\n"
               "<div class=\"revwrap\" style=\"margin-top:0\"><a class=\"revbtn hot\" href=\"/vendre\" title=\"" + tr(lang, "sell_title") + "\">" + tr(lang, "sell_btn") + "</a></div>\n</div>\n"
               "<div class=\"revwrap\"><button id=\"colsToggle\" class=\"revbtn\" "
               "style=\"cursor:pointer;font:inherit;background:rgba(20,28,50,.95);"
               "border:1px solid rgba(77,166,255,.5);color:#e8ecf4\" "
               "title=\"" + tr(lang, "cols_title") + "\"></button></div>\n</header>\n")
    if is_adm:
        out.append("<div class=\"revwrap\"><a class=\"revbtn" + (" hot" if pend else "") + "\" href=\"/review\" title=\"" + tr(lang, "review_title") + "\">" + tr(lang, "review_btn") + (" (" + str(pend) + ")" if pend else "") + "</a></div>\n")
    if not page_items and total == 0:
        out.append("<div class=\"empty\">" + tr(lang, "empty") + "</div>\n")
    else:
        out.append("<div class=\"grid\">\n")
        for i, it in enumerate(page_items):
            cover_file = cover_image(it["images"])
            cover = None
            if cover_file:
                fp = os.path.join(CONFIG["ITEMS_DIR"], it["name"], "images", cover_file)
                try:
                    v = int(os.path.getmtime(fp))
                except OSError:
                    v = 0
                cover = ("/?thumb=" + urllib.parse.quote(it["name"] + "/images/" + cover_file) +
                         "&v=" + str(v) + "&fit=1")
            p = it["price"]
            out.append("<div class=\"card\" onclick=\"openItem(" + str(i) + ")\">\n")
            if is_adm:
                out.append("<button class=\"admindel\" data-name=\"" + h(it["name"]) +
                           "\" title=\"" + tr(lang, "del_title") + "\">\U0001f5d1\ufe0f</button>\n")
            out.append("<div class=\"cover\">")
            if cover:
                out.append("<img loading=\"lazy\" src=\"" + h(cover) + "\" alt=\"\">")
            else:
                out.append("<span class=\"none\">\U0001f4e6</span>")
            out.append("</div>\n<div class=\"cbody\">\n<div class=\"ctitle\">" + h(it["name"]) + "</div>\n")
            if p and p["ok"]:
                out.append("<div class=\"cprice\">" + h(p["text"]) + "</div>\n")
            else:
                out.append("<div class=\"cprice na\">" + tr(lang, "price_na") + "</div>\n")
            out.append("<div class=\"cdesc\">" + h(short_desc(it["desc"])) + "</div>\n")
            if len(it["desc"].strip()) > 160:
                out.append("<span class=\"more\">" + tr(lang, "more") + "</span>\n")
            out.append("</div>\n</div>\n")
        out.append("</div>\n")
        if pages > 1:
            per = CONFIG["ITEMS_PER_PAGE"]
            out.append("<div class=\"pages\">\n")
            if page > 1:
                out.append("<a href=\"/?page=" + str(page - 1) + "\">" + tr(lang, "prev") + "</a>\n")
            frm = max(1, page - 2)
            to = min(pages, page + 2)
            if frm > 1:
                out.append("<a href=\"/?page=1\">1</a><span class=\"dots\">\u2026</span>\n")
            for p in range(frm, to + 1):
                if p == page:
                    out.append("<span class=\"cur\">" + str(p) + "</span>\n")
                else:
                    out.append("<a href=\"/?page=" + str(p) + "\">" + str(p) + "</a>\n")
            if to < pages:
                out.append("<span class=\"dots\">\u2026</span><a href=\"/?page=" + str(pages) + "\">" +
                           str(pages) + "</a>\n")
            if page < pages:
                out.append("<a href=\"/?page=" + str(page + 1) + "\">" + tr(lang, "next") + "</a>\n")
            out.append("</div>\n<div class=\"count\">" + tr(lang, "count").format(
                       a=str((page - 1) * per + 1), b=str(min(total, page * per)), t=str(total)) + "</div>\n")
    out.append("<footer>" + tr(lang, "footer") + "</footer>\n</div>\n"
               "<div id=\"ov\">\n  <div id=\"ovbox\">\n"
               "    <button id=\"ovclose\" onclick=\"closeOv()\">\u2715</button>\n"
               "    <div id=\"ovtitle\"></div>\n    <div id=\"ovprice\"></div>\n"
               "    <img id=\"ovmain\" alt=\"\">\n    <div id=\"ovthumbs\"></div>\n"
               "    <div id=\"ovdesc\"></div>\n    <div id=\"ovcontactwrap\"></div>\n"
               "  </div>\n</div>\n<script>\n"
               "var ITEMS = " + json.dumps(js_items, ensure_ascii=False) + ";\n"
               "var TR = " + json.dumps({
                   "del_confirm": tr(lang, "del_confirm"),
                   "price_na": tr(lang, "price_na"),
                   "no_desc": tr(lang, "no_desc"),
                   "ov_contact": tr(lang, "ov_contact"),
                   "cols_1": tr(lang, "cols_1"),
                   "cols_2": tr(lang, "cols_2"),
                   "cm_err": tr(lang, "cm_err"),
                   "cm_err_net": tr(lang, "cm_err_net"),
                   "cm_files": tr(lang, "cm_files"),
               }, ensure_ascii=False) + ";\n"
               "function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}\n"
               "var VADTOK = " + json.dumps(csrf if is_adm else "") + ";\n"
               "document.querySelectorAll('.admindel').forEach(function(b){\n"
               "  b.addEventListener('click', function(e){\n"
               "    e.stopPropagation();\n"
               "    var name = b.getAttribute('data-name');\n"
               "    if (!confirm(TR.del_confirm.replace('{N}', name))) return;\n"
               "    var f = document.createElement('form');\n"
               "    f.method = 'post'; f.action = location.pathname + location.search;\n"
               "    function inp(n, v){ var i = document.createElement('input'); i.type = 'hidden'; i.name = n; i.value = v; f.appendChild(i); }\n"
               "    inp('del', name); inp('tok', VADTOK); inp('ret', location.pathname + location.search);\n"
               "    document.body.appendChild(f); f.submit();\n"
               "  });\n});\n"
               "function openItem(i){\n"
               "  var it = ITEMS[i]; if(!it) return;\n"
               "  document.getElementById('ovtitle').textContent = it.name;\n"
               "  var pr = document.getElementById('ovprice');\n"
               "  if(it.price && it.price.ok){ pr.textContent = it.price.text; pr.className=''; }\n"
               "  else { pr.textContent = TR.price_na; pr.className='na'; }\n"
               "  pr.id='ovprice';\n"
               "  var main = document.getElementById('ovmain'), th = document.getElementById('ovthumbs');\n"
               "  th.innerHTML='';\n"
               "  var curP = null;\n"
               "  function setMain(o){ curP = o; main.src = '/?preview=' + o.p + '&v=' + o.v; }\n"
               "  if(it.images && it.images.length){\n"
               "    main.style.display='block';\n"
               "    it.images.forEach(function(p,k){\n"
               "      var im=document.createElement('img'); im.src='/?thumb='+p.p+'&v='+p.v; if(k===0)im.className='sel';\n"
               "      im.onclick=function(){ setMain(p);\n"
               "        Array.prototype.forEach.call(th.children,function(c){c.className='';}); im.className='sel'; };\n"
               "      th.appendChild(im);\n"
               "    });\n"
               "    setMain(it.images[0]);\n"
               "  } else { main.style.display='none'; }\n"
               "  document.getElementById('ovdesc').textContent = it.desc || TR.no_desc;\n"
               "  var cw = document.getElementById('ovcontactwrap'); cw.innerHTML='';\n"
               "  if(it.email){\n"
               "    var row=document.createElement('div');\n"
               "    row.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:8px';\n"
               "    var a=document.createElement('a'); a.id='ovcontact'; a.href='mailto:'+it.email;\n"
               "    a.textContent=TR.ov_contact;\n"
               "    var em=document.createElement('span'); em.textContent=it.email;\n"
               "    em.style.cssText='color:#7dd3fc;word-break:break-all;font-size:.95rem';\n"
               "    row.appendChild(a); row.appendChild(em); cw.appendChild(row);\n"
               "  }\n"
               "  document.getElementById('ov').className='open';\n"
               "  document.body.style.overflow='hidden';\n"
               "  _ovAd=it.name; _ovT0=Date.now(); trackEv(it.name,'view');\n"
               "}\n"
               "var _ovAd=null, _ovT0=0;\n"
               "function trackEv(ad,ev){ try{ fetch('/track?ad='+encodeURIComponent(ad)+'&ev='+ev,{keepalive:true}); }catch(e){} }\n"
               "function closeOv(){\n"
               "  document.getElementById('ov').className=''; document.body.style.overflow='';\n"
               "  if(_ovAd && (Date.now()-_ovT0)>5000) trackEv(_ovAd,'read');\n"
               "  _ovAd=null;\n"
               "}\n"
               "document.addEventListener('keydown',function(e){ if(e.key==='Escape') closeOv(); });\n"
               "</script>\n<script>\n"
               "(function(){\n"
               "  var ct=document.getElementById('colsToggle');\n"
               "  if(!ct) return;\n"
               "  function applyCols(){\n"
               "    var c2=localStorage.getItem('vendres-cols')==='2';\n"
               "    document.body.classList.toggle('cols2',c2);\n"
               "    ct.textContent=c2?TR.cols_1:TR.cols_2;\n"
               "  }\n"
               "  ct.onclick=function(){\n"
               "    localStorage.setItem('vendres-cols',document.body.classList.contains('cols2')?'1':'2');\n"
               "    applyCols();\n"
               "  };\n"
               "  applyCols();\n"
               "})();\n</script>\n")
    out.append(render_contact_modal(lang))
    out.append("</body>\n</html>\n")
    return "".join(out)


def render_login(err=False, lang="fr", toggle="/?lang=en"):
    return ("<!DOCTYPE html><html lang=\"" + lang + "\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Admin \u2014 Site</title>"
            "<style>body{background:#0b0b18;color:#dfe8ff;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
            ".card{background:#141428;border:1px solid #444;border-radius:14px;padding:28px;width:320px;text-align:center}"
            "h1{font-size:1.2rem;color:#7dd3fc}input{width:100%;box-sizing:border-box;margin:8px 0;padding:10px;border-radius:8px;border:1px solid #444;background:#0b0b18;color:#fff}"
            "button{width:100%;padding:11px;border:none;border-radius:8px;background:#7dd3fc;color:#0b0b18;font-weight:700;cursor:pointer;font-size:1rem}"
            ".err{color:#f87171;font-size:.85rem;margin-bottom:8px}a{color:#7dd3fc}</style></head><body>"
            "<div class=\"card\"><p style=\"text-align:right;margin:0 0 8px\"><a href=\"" + h(toggle) + "\" title=\"" + h(tr(lang, "lang_title")) + "\">" + tr(lang, "lang_label") + "</a></p><h1>\U0001f527 Admin</h1>"
            + ("<div class=\"err\">" + tr(lang, "login_err") + "</div>" if err else "") +
            "<form method=\"post\"><input name=\"vad_user\" placeholder=\"" + tr(lang, "login_user") + "\" autocomplete=\"username\" autocapitalize=\"off\" autocorrect=\"off\" spellcheck=\"false\">"
            "<input type=\"password\" name=\"vad_pass\" placeholder=\"" + tr(lang, "login_pass") + "\" autocomplete=\"current-password\">"
            "<button name=\"vad_login\" value=\"1\">" + tr(lang, "login_btn") + "</button></form>"
            "<p><a href=\"/\">" + tr(lang, "login_back") + "</a></p></div></html>")


def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(data):
    with FILE_LOCK:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATS_FILE)


def render_stats(data, lang, toggle):
    total_views = sum((s.get("views") or 0) for s in data.values())
    total_reads = sum((s.get("reads") or 0) for s in data.values())
    ordered = sorted(data.items(), key=lambda kv: kv[1].get("views") or 0, reverse=True)
    mx = max([s.get("views") or 0 for s in data.values()] or [0])
    out = ["<!DOCTYPE html>\n<html lang=\"" + lang + "\"><head><meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
           "<title>\U0001f4ca Stats \u2014 Site</title>\n<style>\n"
           "body{background:#0b0b18;color:#dfe8ff;font-family:system-ui,sans-serif;margin:0;padding:20px}\n"
           "h1{color:#7dd3fc;font-size:1.4rem}\n"
           "a{color:#7dd3fc}\n"
           ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}\n"
           ".card{background:#141428;border:1px solid #444;border-radius:12px;padding:14px 20px;min-width:120px;text-align:center}\n"
           ".card b{font-size:1.6rem;color:#fff;display:block}\n"
           ".card span{font-size:.8rem;color:#9aa}\n"
           "table{width:100%;border-collapse:collapse;margin-top:12px;background:#141428;border-radius:12px;overflow:hidden}\n"
           "th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2a2a4a}\n"
           "th{background:#1a1a38;color:#7dd3fc;font-size:.8rem;letter-spacing:.06em}\n"
           "tr:hover td{background:#1a1a38}\n"
           ".num{text-align:right;font-variant-numeric:tabular-nums}\n"
           ".bar{height:8px;background:#2a2a4a;border-radius:4px;overflow:hidden;min-width:80px}\n"
           ".bar i{display:block;height:100%;background:#7dd3fc}\n"
           "</style></head><body>\n"
           + lang_link(toggle, lang) +
           "<h1>" + tr(lang, "stats_h1") + "</h1>\n"
           "<p><a href=\"/\">" + tr(lang, "stats_back") + "</a></p>\n"
           "<div class=\"cards\">\n"
           "  <div class=\"card\"><b>" + str(total_views) + "</b><span>" + tr(lang, "stats_views") + "</span></div>\n"
           "  <div class=\"card\"><b>" + str(total_reads) + "</b><span>" + tr(lang, "stats_reads") + "</span></div>\n"
           "  <div class=\"card\"><b>" + str(len(data)) + "</b><span>" + tr(lang, "stats_ads") + "</span></div>\n"
           "</div>\n"]
    if not data:
        out.append("<p>" + tr(lang, "stats_empty") + "</p>\n")
    else:
        out.append("<table>\n<tr><th>" + tr(lang, "th_ad") + "</th><th class=\"num\">" + tr(lang, "th_views") + "</th>"
                   "<th class=\"num\">" + tr(lang, "th_unique") + "</th><th class=\"num\">" + tr(lang, "th_reads") + "</th>"
                   "<th>" + tr(lang, "th_pop") + "</th><th>" + tr(lang, "th_last") + "</th></tr>\n")
        for ad, s in ordered:
            v = s.get("views") or 0
            u = len(s.get("ips") or {})
            r = s.get("reads") or 0
            pct = round(100 * v / mx) if mx > 0 else 0
            last = (time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last"]))
                    if s.get("last") else "\u2014")
            out.append("<tr>\n  <td>" + h(ad) + "</td>\n  <td class=\"num\">" + str(v) + "</td>\n"
                       "  <td class=\"num\">" + str(u) + "</td>\n  <td class=\"num\">" + str(r) + "</td>\n"
                       "  <td><div class=\"bar\"><i style=\"width:" + str(pct) + "%\"></i></div></td>\n"
                       "  <td>" + last + "</td>\n</tr>\n")
        out.append("</table>\n")
    out.append("<p style=\"color:#9aa;font-size:.85rem;margin-top:16px\">\n"
               "\U0001f4a1 " + tr(lang, "leg_view") + "<br>\n"
               "\U0001f4a1 " + tr(lang, "leg_read") + "<br>\n"
               "\U0001f4a1 " + tr(lang, "leg_unique") + "\n</p>\n")
    out.append("</body></html>\n")
    return "".join(out)


# ------------------------------------------------------------------ courriel
def mail_defaults():
    return {"host": "smtp.gmail.com", "port": 587, "user": "", "pass": "",
            "from": "vous@exemple.com", "from_name": "MonServeur",
            "enabled": True}


def mail_settings():
    cfg = mail_defaults()
    path = CONFIG["MAIL_SETTINGS"]
    if not os.path.isabs(path):
        path = os.path.join(SCRIPT_DIR, path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    return cfg


def load_rl():
    try:
        with open(RL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_rl(data):
    with FILE_LOCK:
        tmp = RL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, RL_FILE)


BAD_EXTS = {"exe", "bat", "cmd", "ps1", "vbs", "js", "lnk", "scr", "msi",
            "com", "pif", "hta", "jar", "dll"}


def contact_send(form, ip, lang="fr"):
    def err(msg):
        return {"ok": False, "err": msg}

    def get(name):
        v = form.get(name, "")
        if isinstance(v, list):
            v = v[0] if v else ""
        return v.strip() if isinstance(v, str) else ""

    name = get("name")
    email = get("email")
    msg = get("message")
    if name == "" or msg == "":
        return err(tr(lang, "ct_req"))
    if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
        return err(tr(lang, "ct_email"))
    if len(msg) > 5000:
        return err(tr(lang, "ct_long"))
    attachments = []
    files = form.get("attach[]", [])
    if not isinstance(files, list):
        files = [files]
    files = [f for f in files if getattr(f, "filename", None)]
    if len(files) > 10:
        return err(tr(lang, "ct_many"))
    total = 0
    for f in files:
        try:
            data = f.file.read()
        except Exception:
            return err(tr(lang, "ct_read"))
        total += len(data)
        if total > 5 * 1024 * 1024:
            return err(tr(lang, "ct_big"))
        ext = os.path.splitext(f.filename)[1][1:].lower()
        if ext in BAD_EXTS:
            return err(tr(lang, "ct_type").format(ext=ext))
        clean = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename))
        attachments.append((clean, f.type or "application/octet-stream", data))
    now = int(time.time())
    rl = load_rl()
    rl = {k: t for k, t in rl.items() if now - int(t) < 3600}
    key = "ip_" + hashlib.md5(ip.encode("utf-8")).hexdigest()
    cnt = sum(1 for k in rl if k.startswith(key))
    if cnt >= 5:
        return err(tr(lang, "ct_rate"))
    rl[key + "_" + str(now)] = now
    save_rl(rl)
    cfg = mail_settings()
    if not cfg.get("enabled"):
        return err(tr(lang, "ct_send"))
    body = ("Name: %s\nEmail: %s\nIP: %s\nDate: %s\n\nMessage:\n%s" %
            (name, email, ip, time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    try:
        em = EmailMessage()
        em["From"] = "%s <%s>" % (cfg.get("from_name", "MonServeur"), cfg["from"])
        em["To"] = CONFIG["CONTACT_TO"]
        em["Subject"] = "Contact Us - exemple.com"
        em.set_content(body)
        for fname, ftype, data in attachments:
            mt, _, st = ftype.partition("/")
            em.add_attachment(data, maintype=mt or "application",
                              subtype=st or "octet-stream", filename=fname)
        srv = smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=15)
        try:
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls()
                srv.ehlo()
            if cfg.get("user"):
                srv.login(cfg["user"], cfg["pass"])
            srv.send_message(em)
        finally:
            try:
                srv.quit()
            except Exception:
                pass
    except Exception as e:
        log("contact-send SMTP erreur: %s" % e)
        return err("Could not send. Try again later.")
    return {"ok": True}


# ------------------------------------------------------------------ serveur HTTP
class _Upload:
    """Petit objet fichier pour le formulaire contact (remplace cgi)."""

    def __init__(self, filename, ctype, data):
        self.filename = filename
        self.type = ctype
        self.file = io.BytesIO(data)


def _parse_multipart(body, boundary):
    """Decoupe un corps multipart/form-data. Retourne un dict nom -> valeur
    (str) ou fichier _Upload (ou liste si plusieurs fichiers meme nom)."""
    form = {}
    if not boundary:
        return form
    delim = b"--" + boundary
    for part in body.split(delim)[1:]:
        if part.startswith(b"--"):
            break
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        head, content = part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        m = re.search(r'name="([^"]*)"', disp)
        if not m:
            continue
        name = m.group(1)
        fm = re.search(r'filename="([^"]*)"', disp)
        if fm is not None and fm.group(1) != "":
            val = _Upload(fm.group(1),
                          headers.get("content-type", "application/octet-stream"),
                          content)
            if name in form:
                prev = form[name]
                form[name] = (prev + [val] if isinstance(prev, list) else [prev, val])
            else:
                form[name] = val
        else:
            form[name] = content.decode("utf-8", "replace")
    return form


# ------------------------------------------------------------------ langue FR/EN
T = {
    "fr": {
        "title": "Site \u2014 Petites annonces",
        "sub": "Petites annonces \u2014 \u00e9lectronique et \u00e9quipement \u00e0 vendre",
        "admin_title": "Administration",
        "admin_logout_title": "Quitter le mode admin",
        "stats_title": "Statistiques des annonces",
        "stats_btn": "\U0001f4ca Stats",
        "contact_title": "Nous contacter",
        "contact_btn": "\u2709\ufe0f Nous contacter",
        "sell_title": "Proposer un article \u00e0 vendre",
        "sell_btn": "\U0001f3f7\ufe0f Vendre un article",
        "review_title": "Annonces en attente",
        "review_btn": "\U0001f3f7\ufe0f Annonces \u00e0 valider",
        "cols_title": "1 ou 2 annonces par ligne",
        "cols_1": "\u2630 1 par ligne",
        "cols_2": "\u229e 2 par ligne",
        "empty": "Aucune annonce pour le moment.<br>Revenez bient\u00f4t !",
        "price_na": "Prix sur demande",
        "more": "Plus \u25b8",
        "prev": "\u2039 Pr\u00e9c\u00e9dent",
        "next": "Suivant \u203a",
        "count": "Annonces {a}\u2013{b} sur {t}",
        "footer": "\u00a9 2026 Site \u2014 Petites annonces \u00b7",
        "del_title": "Supprimer cette annonce",
        "del_confirm": "Supprimer d\u00e9finitivement \u00ab {N} \u00bb ?",
        "no_desc": "Aucune description.",
        "ov_contact": "\u2709\ufe0f Contacter le vendeur",
        "lang_label": "EN",
        "lang_title": "Passer en anglais",
        "cm_title": "\u2709\ufe0f Nous contacter",
        "cm_sub": "Envoyez un message \u00e0 Luc.",
        "cm_sent": "Message envoy\u00e9 !",
        "cm_name": "Votre nom",
        "cm_email": "Votre email",
        "cm_msg": "Votre message",
        "cm_attach": "\U0001f4ce Joindre des fichiers (max 5 Mo total)",
        "cm_send": "Envoyer",
        "cm_err": "Erreur.",
        "cm_err_net": "Erreur r\u00e9seau.",
        "cm_files": "{n} fichier(s) : {names}",
        "login_err": "Identifiants invalides.",
        "login_user": "Utilisateur",
        "login_pass": "Mot de passe",
        "login_btn": "Se connecter",
        "login_back": "\u2190 Retour",
        "stats_h1": "\U0001f4ca Statistiques des annonces",
        "stats_back": "\u2190 Retour \u00e0 Site",
        "stats_views": "Vues totales",
        "stats_reads": "Lectures longues<br>(5+ sec)",
        "stats_ads": "Annonces vues",
        "stats_empty": "Aucune donn\u00e9e pour l'instant. Les vues sont compt\u00e9es quand un visiteur ouvre une annonce.",
        "th_ad": "Annonce",
        "th_views": "Vues",
        "th_unique": "Visiteurs uniques",
        "th_reads": "Lectures",
        "th_pop": "Populaire",
        "th_last": "Derni\u00e8re vue",
        "leg_view": "<b>Vue</b> = un visiteur a ouvert l'annonce.",
        "leg_read": "<b>Lecture longue</b> = rest\u00e9 5+ secondes sur la description.",
        "leg_unique": "<b>Visiteurs uniques</b> = adresses IP distinctes.",
        "sell_page_title": "Vendre un article \u2014 Site",
        "sell_h1": "\U0001f3f7\ufe0f Vendre un article",
        "sell_note": "Remplis ce formulaire \u2014 je vais v\u00e9rifier ton annonce et la publier sur Site si tout est bon.",
        "sell_name": "Nom de l'article",
        "sell_name_ph": "ex. Lampe de plancher vintage",
        "sell_desc": "Description",
        "sell_desc_ph": "\u00c9tat, d\u00e9tails, infos de ramassage...",
        "sell_price": "Prix (chiffres seulement)",
        "sell_price_ph": "ex. 25",
        "sell_email": "Ton courriel",
        "sell_email_ph": "toi@exemple.com",
        "sell_photos": "Photos (max 5)",
        "sell_cover": "Couverture",
        "sell_send": "Envoyer pour v\u00e9rification",
        "sell_back": "\u2190 Retour \u00e0 Vendres",
        "sell_ok_title": "Annonce re\u00e7ue",
        "sell_ok": "Merci \u2014 ton annonce est en attente de v\u00e9rification. Si tout est bon, elle appara\u00eetra bient\u00f4t sur Site.",
        "sell_err_name": "Le nom de l'article doit avoir entre 3 et 80 caract\u00e8res.",
        "sell_err_desc": "La description doit avoir entre 10 et 2000 caract\u00e8res.",
        "sell_err_price": "Le prix semble invalide \u2014 chiffres seulement, ex. 25.",
        "sell_err_email": "Cette adresse courriel semble invalide.",
        "sell_err_folder": "Impossible de cr\u00e9er un dossier \u00e0 partir de ce nom.",
        "sell_err_save": "Impossible d'enregistrer \u2014 r\u00e9essaie plus tard.",
        "rev_title": "\U0001f3f7\ufe0f Annonces \u00e0 valider ({n} en attente)",
        "rev_back": "\u2190 Retour \u00e0 Vendres",
        "rev_empty": "Rien en attente. Les nouvelles propositions du formulaire de vente appara\u00eetront ici.",
        "rev_from": "de",
        "rev_pub": "\u2705 Publier",
        "rev_pub_confirm": "Publier cette annonce sur Site ?",
        "rev_del": "\U0001f5d1\ufe0f Rejeter",
        "rev_del_confirm": "Supprimer cette proposition ?",
        "ct_req": "Nom et message requis.",
        "ct_email": "Adresse courriel invalide.",
        "ct_long": "Message trop long.",
        "ct_many": "Trop de fichiers (max 10).",
        "ct_read": "Impossible de lire le fichier.",
        "ct_big": "Fichiers trop gros (max 5 Mo au total).",
        "ct_type": "Type de fichier non autoris\u00e9 : {ext}",
        "ct_rate": "Trop de messages. R\u00e9essaie plus tard.",
        "ct_send": "Impossible d'envoyer. R\u00e9essaie plus tard.",
    },
    "en": {
        "title": "Site \u2014 Classified ads",
        "sub": "Classified ads \u2014 electronics and equipment for sale",
        "admin_title": "Administration",
        "admin_logout_title": "Exit admin mode",
        "stats_title": "Ad statistics",
        "stats_btn": "\U0001f4ca Stats",
        "contact_title": "Contact us",
        "contact_btn": "\u2709\ufe0f Contact Us",
        "sell_title": "List an item for sale",
        "sell_btn": "\U0001f3f7\ufe0f Sell an item",
        "review_title": "Pending ads",
        "review_btn": "\U0001f3f7\ufe0f Ads to review",
        "cols_title": "1 or 2 ads per row",
        "cols_1": "\u2630 1 per row",
        "cols_2": "\u229e 2 per row",
        "empty": "No ads yet.<br>Check back soon!",
        "price_na": "Price on request",
        "more": "More \u25b8",
        "prev": "\u2039 Previous",
        "next": "Next \u203a",
        "count": "Ads {a}\u2013{b} of {t}",
        "footer": "\u00a9 2026 Site \u2014 Classified ads \u00b7",
        "del_title": "Delete this ad",
        "del_confirm": "Permanently delete \u201c{N}\u201d?",
        "no_desc": "No description.",
        "ov_contact": "\u2709\ufe0f Contact the seller",
        "lang_label": "FR",
        "lang_title": "Switch to French",
        "cm_title": "\u2709\ufe0f Contact Us",
        "cm_sub": "Send a message to Luc.",
        "cm_sent": "Message sent!",
        "cm_name": "Your name",
        "cm_email": "Your email",
        "cm_msg": "Your message",
        "cm_attach": "\U0001f4ce Attach files (max 5 MB total)",
        "cm_send": "Send",
        "cm_err": "Error.",
        "cm_err_net": "Network error.",
        "cm_files": "{n} file(s): {names}",
        "login_err": "Invalid credentials.",
        "login_user": "Username",
        "login_pass": "Password",
        "login_btn": "Log in",
        "login_back": "\u2190 Back",
        "stats_h1": "\U0001f4ca Ad statistics",
        "stats_back": "\u2190 Back to Site",
        "stats_views": "Total views",
        "stats_reads": "Long reads<br>(5+ sec)",
        "stats_ads": "Ads viewed",
        "stats_empty": "No data yet. Views are counted when a visitor opens an ad.",
        "th_ad": "Ad",
        "th_views": "Views",
        "th_unique": "Unique visitors",
        "th_reads": "Reads",
        "th_pop": "Popularity",
        "th_last": "Last viewed",
        "leg_view": "<b>View</b> = a visitor opened the ad.",
        "leg_read": "<b>Long read</b> = stayed 5+ seconds on the description.",
        "leg_unique": "<b>Unique visitors</b> = distinct IP addresses.",
        "sell_page_title": "Sell an item \u2014 Site",
        "sell_h1": "\U0001f3f7\ufe0f Sell an item",
        "sell_note": "Fill this in \u2014 I'll review it and publish your ad on Site if all looks good.",
        "sell_name": "Item name",
        "sell_name_ph": "e.g. Vintage floor lamp",
        "sell_desc": "Description",
        "sell_desc_ph": "Condition, details, pickup info...",
        "sell_price": "Price (numbers only)",
        "sell_price_ph": "e.g. 25",
        "sell_email": "Your email",
        "sell_email_ph": "you@example.com",
        "sell_photos": "Photos (up to 5)",
        "sell_cover": "Cover",
        "sell_send": "Send for review",
        "sell_back": "\u2190 Back to Vendres",
        "sell_ok_title": "Ad received",
        "sell_ok": "Thanks \u2014 your ad is waiting for review. If all looks good it will appear on Site soon.",
        "sell_err_name": "Item name must be 3\u201380 characters.",
        "sell_err_desc": "Description must be 10\u20132000 characters.",
        "sell_err_price": "Price looks wrong \u2014 numbers only, e.g. 25.",
        "sell_err_email": "That email address looks invalid.",
        "sell_err_folder": "Could not make a folder from that name.",
        "sell_err_save": "Could not save \u2014 please try again later.",
        "rev_title": "\U0001f3f7\ufe0f Ads review queue ({n} pending)",
        "rev_back": "\u2190 Back to Vendres",
        "rev_empty": "Nothing waiting. New submissions from the sell form will appear here.",
        "rev_from": "from",
        "rev_pub": "\u2705 Publish",
        "rev_pub_confirm": "Publish this ad on Site?",
        "rev_del": "\U0001f5d1\ufe0f Reject",
        "rev_del_confirm": "Delete this submission?",
        "ct_req": "Name and message are required.",
        "ct_email": "Invalid email address.",
        "ct_long": "Message too long.",
        "ct_many": "Too many files (max 10).",
        "ct_read": "Could not read file.",
        "ct_big": "Files too large (max 5 MB total).",
        "ct_type": "File type not allowed: {ext}",
        "ct_rate": "Too many messages. Try again later.",
        "ct_send": "Could not send. Try again later.",
    },
}


def tr(lang, key):
    d = T.get(lang) or T["fr"]
    return d.get(key) or T["fr"].get(key) or key


def cookie_lang(ck):
    v = (ck or {}).get("vlang", "")
    return v if v in ("fr", "en") else "fr"


def toggle_href(path, q, lang):
    other = "en" if lang == "fr" else "fr"
    qq = {k: v for k, v in (q or {}).items() if k != "lang"}
    qq["lang"] = other
    return path + "?" + urllib.parse.urlencode(qq)


def lang_btn(toggle, lang):
    return ("<div class=\"revwrap\" style=\"margin-top:0\"><a class=\"revbtn\" href=\"" + h(toggle) + "\" title=\"" + h(tr(lang, "lang_title")) + "\" style=\"color:#e8ecf4;border-color:rgba(125,211,252,.6)\">\U0001f310 " + tr(lang, "lang_label") + "</a></div>\n")


def lang_link(toggle, lang):
    return ("<div style=\"text-align:right;max-width:640px;margin:0 auto 10px\">"
            "<a href=\"" + h(toggle) + "\" title=\"" + h(tr(lang, "lang_title")) + "\" "
            "style=\"color:#7dd3fc;text-decoration:none;border:1px solid rgba(125,211,252,.4);"
            "border-radius:8px;padding:6px 14px;font-size:.85rem\">" + tr(lang, "lang_label") + "</a></div>")


# ------------------------------------------------------------------ vendre un article
def pending_dir():
    return os.path.join(CONFIG["ITEMS_DIR"], "_PENDING")


def pending_items():
    items = []
    pd = pending_dir()
    try:
        names = os.listdir(pd)
    except OSError:
        return items
    for f in names:
        if f.startswith("_") or f.startswith("."):
            continue
        full = os.path.join(pd, f)
        if not os.path.isdir(full):
            continue
        jf = os.path.join(full, "ad.json")
        if not os.path.isfile(jf):
            continue
        try:
            with open(jf, "r", encoding="utf-8") as fh:
                ad = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(ad, dict) and ad.get("name"):
            ad["_id"] = f
            items.append(ad)
    items.sort(key=lambda a: a.get("date", ""), reverse=True)
    return items


def pending_count():
    return len(pending_items())


def valid_pending_id(pid):
    return (isinstance(pid, str) and pid != "" and "/" not in pid and
            "\\" not in pid and ".." not in pid and
            os.path.isdir(os.path.join(pending_dir(), pid)))


def safe_folder_name(name):
    folder = "".join(c for c in name if c.isalnum() or c in " _-.")
    folder = re.sub(r"\s+", " ", folder).strip(" .")
    return folder[:60]


def sanitize_stem(name):
    safe = re.sub(r"[^A-Za-z0-9_\-\.]", "_", name)[:40]
    return safe if safe else "photo"


ALLOWED_IMG_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}


def is_real_image(data, ext):
    if ext == "webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    return False


def get_text(form, name):
    v = form.get(name, "")
    if isinstance(v, list):
        v = v[0] if v else ""
    return v.strip() if isinstance(v, str) else ""


SELL_CSS = """
body{background:#050514;color:#dfe8ff;font-family:system-ui,sans-serif;margin:0;padding:20px}
.card{max-width:640px;margin:0 auto;background:rgba(10,10,28,.96);border:1px solid rgba(180,160,255,.5);border-radius:14px;padding:26px 24px}
h1{color:#7dd3fc;font-size:1.3rem;text-align:center;margin-bottom:6px}
.note{text-align:center;color:#9fb3d1;font-size:.85rem;margin-bottom:14px}
label{display:block;margin-bottom:12px;color:#cfe6ff;font-size:.88rem}
input,textarea{width:100%;margin-top:5px;padding:10px;border-radius:8px;border:1px solid rgba(120,170,255,.3);background:rgba(5,8,20,.8);color:#eaf2ff;font-size:.95rem;box-sizing:border-box;font-family:inherit}
textarea{resize:vertical}
.hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
.sell-btn{display:block;width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(135deg,#7dd3fc,#a78bfa);color:#0a0a20;font-weight:700;font-size:1rem;cursor:pointer;margin-top:4px}
.sell-btn:hover{filter:brightness(1.12)}
a{color:#7dd3fc}
#photo-prev{display:flex;gap:10px;flex-wrap:wrap;margin:2px 0 12px}
.ppick{text-align:center}
.ppick img{width:80px;height:80px;object-fit:cover;border-radius:8px;border:1px solid rgba(120,170,255,.3);display:block}
.ppick label{font-size:.75rem;color:#9fb3d1;display:block;margin-top:4px;cursor:pointer}
.ppick input{width:auto;margin:0 4px 0 0;vertical-align:middle}
"""


def render_sell_form(lang="fr", toggle="/?lang=en", values=None, error=None):
    values = values or {}
    return ("<!DOCTYPE html><html lang=\"" + lang + "\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>" + tr(lang, "sell_page_title") + "</title><style>" + SELL_CSS + "</style></head><body>"
            + lang_link(toggle, lang) +
            "<div class=\"card\"><h1>" + tr(lang, "sell_h1") + "</h1>"
            "<p class=\"note\">" + tr(lang, "sell_note") + "</p>"
            + ("<p style=\"color:#f87171;text-align:center;font-size:.9rem\">\u26a0\ufe0f " + h(error) + "</p>" if error else "") +
            "<form method=\"post\" enctype=\"multipart/form-data\" id=\"sell-form\">"
            "<label>" + tr(lang, "sell_name") + "<input name=\"name\" value=\"" + h(values.get("name", "")) + "\" required maxlength=\"80\" placeholder=\"" + tr(lang, "sell_name_ph") + "\" autocomplete=\"off\"></label>"
            "<label>" + tr(lang, "sell_desc") + "<textarea name=\"description\" required maxlength=\"2000\" rows=\"4\" placeholder=\"" + tr(lang, "sell_desc_ph") + "\">" + h(values.get("description", "")) + "</textarea></label>"
            "<label>" + tr(lang, "sell_price") + "<input name=\"price\" value=\"" + h(values.get("price", "")) + "\" required maxlength=\"10\" placeholder=\"" + tr(lang, "sell_price_ph") + "\" autocomplete=\"off\"></label>"
            "<label>" + tr(lang, "sell_email") + "<input type=\"email\" name=\"email\" value=\"" + h(values.get("email", "")) + "\" required maxlength=\"120\" placeholder=\"" + tr(lang, "sell_email_ph") + "\" autocomplete=\"off\"></label>"
            "<label>" + tr(lang, "sell_photos") + "<input type=\"file\" name=\"images[]\" accept=\"image/*\" multiple></label>"
            "<div id=\"photo-prev\"></div>"
            "<input type=\"text\" name=\"website\" class=\"hp\" autocomplete=\"off\" tabindex=\"-1\" aria-hidden=\"true\">"
            "<button type=\"submit\" class=\"sell-btn\">" + tr(lang, "sell_send") + "</button>"
            "</form>"
            "<p style=\"text-align:center;margin-top:14px\"><a href=\"/\">" + tr(lang, "sell_back") + "</a></p></div>"
            "<script>(function(){var fi=document.querySelector('#sell-form input[type=file]');"
            "var pv=document.getElementById('photo-prev');if(!fi||!pv)return;"
            "fi.addEventListener('change',function(){pv.innerHTML='';var files=fi.files;"
            "for(var i=0;i<files.length;i++){if(files[i].type.indexOf('image/')!==0)continue;"
            "var d=document.createElement('div');d.className='ppick';"
            "var img=document.createElement('img');img.src=URL.createObjectURL(files[i]);"
            "var lab=document.createElement('label');var r=document.createElement('input');"
            "r.type='radio';r.name='cover_idx';r.value=i;if(i===0)r.checked=true;"
            "lab.appendChild(r);lab.appendChild(document.createTextNode('" + tr(lang, "sell_cover") + "'));"
            "d.appendChild(img);d.appendChild(lab);pv.appendChild(d);}});})();</script>"
            "</body></html>")


def render_sell_message(title, body):
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>" + h(title) + "</title><style>"
            "body{background:#050514;color:#dfe8ff;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}"
            ".card{max-width:520px;background:rgba(10,10,28,.96);border:1px solid rgba(180,160,255,.5);border-radius:14px;padding:30px;text-align:center}"
            "h1{color:#7dd3fc;font-size:1.3rem}a{color:#7dd3fc}</style></head><body>"
            "<div class=\"card\">" + body + "</div></body></html>")


def render_review(items, csrf, lang, toggle):
    out = ["<!DOCTYPE html><html lang=\"" + lang + "\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           "<title>" + tr(lang, "review_btn") + "</title><style>"
           "body{background:#050514;color:#dfe8ff;font-family:system-ui,sans-serif;margin:0;padding:20px}"
           "h1{color:#7dd3fc;font-size:1.3rem;text-align:center}"
           ".ad{max-width:640px;margin:14px auto;background:rgba(10,10,28,.96);border:1px solid rgba(180,160,255,.5);border-radius:12px;padding:18px}"
           ".ad h2{margin:0 0 6px;color:#cfe6ff;font-size:1.05rem}"
           ".ad .price{color:#7dd3fc;font-weight:700}"
           ".ad .meta{color:#8fa1b8;font-size:.78rem;margin:8px 0}"
           ".ad p.desc{white-space:pre-wrap;font-size:.92rem}"
           ".row{display:flex;gap:10px;margin-top:10px}"
           ".row button{flex:1;padding:10px;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-size:.95rem}"
           ".pub{background:#34d399;color:#06281c}.del{background:#f87171;color:#3b0a0a}"
           ".empty{text-align:center;color:#8fa1b8;margin-top:40px}"
           ".backbtn{display:inline-block;padding:8px 20px;border-radius:10px;background:rgba(125,211,252,.12);color:#7dd3fc;text-decoration:none;border:1px solid rgba(125,211,252,.4);font-size:.9rem}"
           ".backbtn:hover{background:rgba(125,211,252,.25)}"
           ".thumbs{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}"
           ".thumbs img{width:90px;height:90px;object-fit:cover;border-radius:8px;border:1px solid rgba(180,160,255,.4)}"
           "</style></head><body>"
           + lang_link(toggle, lang) +
           "<h1>" + tr(lang, "rev_title").format(n=len(items)) + "</h1>"
           "<p style=\"text-align:center\"><a class=\"backbtn\" href=\"/\">" + tr(lang, "rev_back") + "</a></p>"]
    if not items:
        out.append("<p class=\"empty\">" + tr(lang, "rev_empty") + "</p>")
    for ad in items:
        pid = ad["_id"]
        photos = []
        phdir = os.path.join(pending_dir(), pid, "images")
        try:
            for pf in os.listdir(phdir):
                if re.search(r"\.(jpg|jpeg|png|webp|gif)$", pf, re.I):
                    photos.append(pf)
        except OSError:
            pass
        photos.sort(key=natkey)
        out.append("<div class=\"ad\"><h2>" + h(ad.get("name", "")) + "</h2>")
        price = ad.get("price", "")
        out.append("<div class=\"price\">" + ("$" + h(price) if price != "" else "") + "</div>")
        out.append("<p class=\"desc\">" + h(ad.get("description", "")) + "</p>")
        if photos:
            out.append("<div class=\"thumbs\">")
            for pf in photos:
                out.append("<img src=\"/review?pimg=" + urllib.parse.quote(pid) + "/" + urllib.parse.quote(pf) + "\" alt=\"\">")
            out.append("</div>")
        out.append("<div class=\"meta\">" + tr(lang, "rev_from") + " " + h(ad.get("email", "")) + " \u00b7 " + h(ad.get("ip", "")) + " \u00b7 " + h(ad.get("date", "")) + "</div>")
        out.append("<form method=\"post\" class=\"row\">"
                   "<input type=\"hidden\" name=\"id\" value=\"" + h(pid) + "\">"
                   "<input type=\"hidden\" name=\"tok\" value=\"" + h(csrf) + "\">"
                   "<button class=\"pub\" name=\"action\" value=\"publish\" onclick=\"return confirm('" + tr(lang, "rev_pub_confirm") + "')\">" + tr(lang, "rev_pub") + "</button>"
                   "<button class=\"del\" name=\"action\" value=\"delete\" onclick=\"return confirm('" + tr(lang, "rev_del_confirm") + "')\">" + tr(lang, "rev_del") + "</button>"
                   "</form></div>")
    out.append("</body></html>")
    return "".join(out)


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import socket
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Vendres/" + VERSION

    def log_message(self, fmt, *args):
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                        self.client_address[0],
                                        self.command, self.path.split("?")[0]))
        except Exception:
            pass

    # --- reponses ---
    def send_bytes(self, body, ctype="text/html; charset=utf-8", status=200,
                   extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj), "application/json", status)

    def redirect(self, location, cookie=None):
        extra = {"Location": location}
        if cookie is not None:
            extra["Set-Cookie"] = cookie
        self.send_bytes(b"", "text/html; charset=utf-8", 302, extra)

    def cookies(self):
        out = {}
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            for k, m in jar.items():
                out[k] = m.value
        except Exception:
            pass
        return out

    def query(self):
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        return parts.path, {k: v[0] for k, v in qs.items()}

    def read_form(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=([^;]+)", ctype)
            boundary = m.group(1).strip().strip('"') if m else ""
            return _parse_multipart(raw, boundary.encode("utf-8"))
        qs = urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                   keep_blank_values=True)
        return {k: v[0] for k, v in qs.items()}

    # --- GET ---
    def do_GET(self):
        path, q = self.query()
        if q.get("lang") in ("fr", "en"):
            qq = {k: v for k, v in q.items() if k != "lang"}
            qs = urllib.parse.urlencode(qq)
            self.redirect(path + ("?" + qs if qs else ""),
                          "vlang=" + q["lang"] + "; Path=/; Max-Age=31536000; SameSite=Lax")
            return
        lang = cookie_lang(self.cookies())
        if "thumb" in q or "preview" in q or "img" in q:
            self.serve_image(q)
            return
        if path == "/stats":
            if not is_admin(self.cookies()):
                self.redirect("/?admin=login&ret=/stats")
                return
            self.send_bytes(render_stats(load_stats(), lang, toggle_href("/stats", {}, lang)))
            return
        if path == "/track":
            self.serve_track(q)
            return
        if path == "/vendre":
            self.send_bytes(render_sell_form(lang, toggle_href("/vendre", {}, lang)))
            return
        if path == "/review":
            ck = self.cookies()
            if not is_admin(ck):
                self.redirect("/?admin=login&ret=/review")
                return
            if "pimg" in q:
                self.serve_pending_image(q["pimg"])
                return
            self.send_bytes(render_review(pending_items(), vad_csrf(ck.get("vadm", "")), lang, toggle_href("/review", {}, lang)))
            return
        if path != "/":
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)
            return
        ck = self.cookies()
        adm = is_admin(ck)
        if q.get("admin") == "logout":
            import email.utils
            self.redirect("/", "vadm=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; HttpOnly")
            return
        if q.get("admin") == "login" and not adm:
            self.send_bytes(render_login(False, lang, toggle_href("/", q, lang)))
            return
        items = scan_items()
        per = CONFIG["ITEMS_PER_PAGE"]
        total = len(items)
        pages = max(1, (total + per - 1) // per)
        try:
            page = int(q.get("page", "1"))
        except ValueError:
            page = 1
        page = max(1, min(page, pages))
        page_items = items[(page - 1) * per:page * per]
        js_items = []
        for it in page_items:
            imgs = []
            for f in it["images"]:
                fp = os.path.join(CONFIG["ITEMS_DIR"], it["name"], "images", f)
                try:
                    v = int(os.path.getmtime(fp))
                except OSError:
                    v = 0
                imgs.append({"p": urllib.parse.quote(it["name"] + "/images/" + f), "v": v})
            js_items.append({"name": it["name"], "desc": it["desc"],
                             "price": it["price"], "email": it["email"],
                             "images": imgs})
        self.send_bytes(render_listing(page_items, page, pages, total, adm,
                                       pending_count(),
                                       vad_csrf(ck.get("vadm", "")) if adm else "",
                                       js_items, lang, toggle_href("/", q, lang)))

    def serve_image(self, q):
        if "thumb" in q:
            mode, rel = "thumb", q["thumb"]
        elif "preview" in q:
            mode, rel = "preview", q["preview"]
        else:
            mode, rel = "img", q["img"]
        path = resolve_item_file(urllib.parse.unquote(rel))
        if path is False:
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)
            return
        if mode != "img":
            w, hgt = (480, 270) if mode == "thumb" else (1280, 720)
            crop = mode == "thumb" and "fit" not in q
            th = sized_image(path, w, hgt, crop)
            if th is not False:
                self.send_file(th, "image/jpeg")
                return
        ext = os.path.splitext(path)[1][1:].lower()
        self.send_file(path, IMG_EXTS[ext])

    def send_file(self, path, ctype):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 65536)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)

    def serve_track(self, q):
        ad = q.get("ad", "")
        ev = q.get("ev", "view")
        ok = bool(ad) and re.match(
            r"^[A-Za-z0-9 _\-\.\u00e9\u00e8\u00ea\u00e0\u00e2\u00ee\u00f4\u00fb\u00e7"
            r"\u00c9\u00c8\u00ca\u00c0\u00c2\u00ce\u00d4\u00db\u00c7]+$", ad)
        if not ok:
            self.send_json({"ok": False}, 200)
            return
        ip = self.client_address[0]
        day = time.strftime("%Y-%m-%d")
        now = int(time.time())
        data = load_stats()
        entry = data.get(ad)
        if entry is None:
            entry = {"views": 0, "reads": 0, "ips": {}, "last": 0, "daily": {}}
            data[ad] = entry
        entry["views"] = (entry.get("views") or 0) + 1
        ips = entry.get("ips") or {}
        ips[ip] = (ips.get(ip) or 0) + 1
        entry["ips"] = ips
        entry["last"] = now
        daily = entry.get("daily") or {}
        daily[day] = (daily.get(day) or 0) + 1
        if len(daily) > 90:
            daily = dict(sorted(daily.items())[-90:])
        entry["daily"] = daily
        if ev == "read":
            entry["reads"] = (entry.get("reads") or 0) + 1
        save_stats(data)
        self.send_json({"ok": True})

    def serve_pending_image(self, rel):
        parts = rel.split("/", 1)
        pid = parts[0] if parts else ""
        pfile = parts[1] if len(parts) > 1 else ""
        if (not valid_pending_id(pid) or
                not re.match(r"^[A-Za-z0-9_\-\.]+\.(jpg|jpeg|png|webp|gif)$", pfile, re.I)):
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)
            return
        fp = os.path.join(pending_dir(), pid, "images", os.path.basename(pfile))
        if not os.path.isfile(fp):
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)
            return
        ext = os.path.splitext(fp)[1][1:].lower()
        self.send_file(fp, IMG_EXTS.get(ext, "application/octet-stream"))

    def serve_sell_submit(self, lang):
        form = self.read_form()

        def err_page(msg):
            self.send_bytes(render_sell_form(
                lang, "/vendre?lang=" + ("en" if lang == "fr" else "fr"),
                {"name": name, "description": desc, "price": price_raw, "email": email}, msg))

        def done_page():
            self.send_bytes(render_sell_message(
                tr(lang, "sell_ok_title"), "<h1>\u2705 " + tr(lang, "sell_ok_title") + " !</h1><p>" + tr(lang, "sell_ok") + "</p>"
                "<p><a href=\"/\">" + tr(lang, "sell_back") + "</a></p>"))

        if get_text(form, "website") != "":
            done_page()
            return
        name = get_text(form, "name")
        desc = get_text(form, "description")
        price_raw = get_text(form, "price")
        email = get_text(form, "email")
        if len(name) < 3 or len(name) > 80:
            err_page(tr(lang, "sell_err_name"))
            return
        if len(desc) < 10 or len(desc) > 2000:
            err_page(tr(lang, "sell_err_desc"))
            return
        price = re.sub(r"\D", "", price_raw)
        if price == "" or len(price) > 10:
            err_page(tr(lang, "sell_err_price"))
            return
        if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", email):
            err_page(tr(lang, "sell_err_email"))
            return
        ip = self.client_address[0]
        pd = pending_dir()
        try:
            os.makedirs(pd, exist_ok=True)
        except OSError:
            pass
        rlf = os.path.join(pd, "_ratelimit.json")
        try:
            with open(rlf, "r", encoding="utf-8") as fh:
                rl = json.load(fh)
            if not isinstance(rl, dict):
                rl = {}
        except (OSError, ValueError):
            rl = {}
        now = int(time.time())
        times = [t for t in (rl.get(ip) or []) if now - int(t) < 3600]
        if len(times) >= 5:
            done_page()
            return
        times.append(now)
        rl[ip] = times
        with FILE_LOCK:
            tmp = rlf + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rl, fh)
            os.replace(tmp, rlf)
        folder = safe_folder_name(name)
        if folder == "":
            err_page(tr(lang, "sell_err_folder"))
            return
        dest = os.path.join(pd, folder)
        i = 2
        while os.path.isdir(dest):
            dest = os.path.join(pd, folder + " (" + str(i) + ")")
            i += 1
        try:
            os.makedirs(os.path.join(dest, "images"), exist_ok=True)
        except OSError:
            err_page(tr(lang, "sell_err_save"))
            return
        try:
            cover_idx = int(get_text(form, "cover_idx") or 0)
        except ValueError:
            cover_idx = 0
        files = form.get("images[]", [])
        if not isinstance(files, list):
            files = [files]
        files = [f for f in files if getattr(f, "filename", None)]
        for i, f in enumerate(files[:5]):
            try:
                data = f.file.read()
            except Exception:
                continue
            if len(data) > 8 * 1024 * 1024:
                continue
            ext = os.path.splitext(f.filename)[1][1:].lower()
            if ext not in ALLOWED_IMG_EXTS:
                continue
            if not is_real_image(data, ext):
                continue
            if i == cover_idx:
                target = os.path.join(dest, "images", "0." + ext)
            else:
                stem = sanitize_stem(os.path.splitext(os.path.basename(f.filename))[0])
                target = os.path.join(dest, "images", stem + "." + ext)
                k = 2
                while os.path.exists(target):
                    target = os.path.join(dest, "images", stem + "-" + str(k) + "." + ext)
                    k += 1
            try:
                with open(target, "wb") as fh:
                    fh.write(data)
            except OSError:
                continue
        ad = {"name": name, "description": desc, "price": price,
              "email": email, "ip": ip, "date": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(os.path.join(dest, "ad.json"), "w", encoding="utf-8") as fh:
                json.dump(ad, fh, ensure_ascii=False, indent=2)
        except OSError:
            err_page(tr(lang, "sell_err_save"))
            return
        log("sell: nouvelle annonce en attente: %s" % folder)
        done_page()

    # --- POST ---
    def do_POST(self):
        path, _q = self.query()
        lang = cookie_lang(self.cookies())
        if path == "/contact-send":
            form = self.read_form()
            self.send_json(contact_send(form, self.client_address[0], lang))
            return
        if path == "/vendre":
            self.serve_sell_submit(lang)
            return
        if path == "/review":
            ck = self.cookies()
            if not is_admin(ck):
                self.redirect("/?admin=login&ret=/review")
                return
            form = self.read_form()
            pid = str(form.get("id", ""))
            tok = str(form.get("tok", ""))
            action = str(form.get("action", ""))
            if (hmac.compare_digest(vad_csrf(ck.get("vadm", "")), tok) and valid_pending_id(pid)):
                src = os.path.join(pending_dir(), pid)
                if action == "delete":
                    rrmdir(src)
                    log("review: annonce refusee: %s" % pid)
                elif action == "publish":
                    try:
                        with open(os.path.join(src, "ad.json"), "r", encoding="utf-8") as fh:
                            ad = json.load(fh)
                    except (OSError, ValueError):
                        ad = None
                    if isinstance(ad, dict) and ad.get("name"):
                        dest = os.path.join(CONFIG["ITEMS_DIR"], pid)
                        n = 2
                        while os.path.isdir(dest) or os.path.exists(dest):
                            dest = os.path.join(CONFIG["ITEMS_DIR"], pid + " (" + str(n) + ")")
                            n += 1
                        try:
                            os.makedirs(dest, exist_ok=True)
                        except OSError:
                            dest = None
                        if dest:
                            try:
                                with open(os.path.join(dest, "description.txt"), "w", encoding="utf-8") as fh:
                                    fh.write(ad.get("description", ""))
                                with open(os.path.join(dest, "price.txt"), "w", encoding="utf-8") as fh:
                                    fh.write(ad.get("price", ""))
                                with open(os.path.join(dest, "contact.txt"), "w", encoding="utf-8") as fh:
                                    fh.write(ad.get("email", ""))
                            except OSError:
                                pass
                            simg = os.path.join(src, "images")
                            if os.path.isdir(simg):
                                try:
                                    shutil.move(simg, os.path.join(dest, "images"))
                                except OSError:
                                    pass
                            else:
                                try:
                                    os.makedirs(os.path.join(dest, "images"), exist_ok=True)
                                except OSError:
                                    pass
                            rrmdir(src)
                            log("review: annonce publiee: %s" % pid)
            self.redirect("/review")
            return
        if path != "/":
            self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)
            return
        form = self.read_form()
        ck = self.cookies()
        if "vad_login" in form:
            vu = str(form.get("vad_user", ""))
            vpw = str(form.get("vad_pass", ""))
            if (CONFIG["ADMIN_PASS"] and
                    hmac.compare_digest(CONFIG["ADMIN_USER"].lower(), vu.lower()) and
                    hmac.compare_digest(str(CONFIG["ADMIN_PASS"]), vpw)):
                import email.utils
                exp = int(time.time()) + 30 * 86400
                cookie = ("vadm=%s; Expires=%s; Path=/; HttpOnly" %
                          (vad_cookie_val(exp), email.utils.formatdate(exp, usegmt=True)))
                ret = str(_q.get("ret", "/"))
                if ret == "" or (not ret.startswith("?") and
                                 not ret.startswith("./") and not ret.startswith("/")):
                    ret = "/"
                self.redirect(ret, cookie)
            else:
                self.send_bytes(render_login(True, lang, toggle_href("/", {"admin": "login"}, lang)))
            return
        if "del" in form and is_admin(ck):
            dname = str(form.get("del", ""))
            dtok = str(form.get("tok", ""))
            if (hmac.compare_digest(vad_csrf(ck.get("vadm", "")), dtok) and
                    dname != "" and "/" not in dname and "\\" not in dname and
                    not dname.startswith("_") and not dname.startswith(".") and
                    dname != ".." and
                    os.path.isdir(os.path.join(CONFIG["ITEMS_DIR"], dname))):
                rrmdir(os.path.join(CONFIG["ITEMS_DIR"], dname))
                log("admin: annonce supprimee: %s" % dname)
            ret = str(form.get("ret", "/"))
            if ret == "" or (not ret.startswith("?") and
                             not ret.startswith("./") and not ret.startswith("/")):
                ret = "/"
            self.redirect(ret)
            return
        self.send_bytes("Introuvable", "text/plain; charset=utf-8", 404)

    def do_HEAD(self):
        self.do_GET()


def main():
    res_purge = purger_journal(LOG_FILE, CONFIG["PURGE_JOURS"])
    if res_purge:
        print("Purge journal : %d ligne(s) gardee(s), %d effacee(s) (%d derniers jours)"
              % (res_purge[0], res_purge[1], CONFIG["PURGE_JOURS"]))
    if not CONFIG["ADMIN_PASS"]:
        print("ATTENTION: ADMIN_PASS vide dans vendres-config.json — "
              "la connexion admin est desactivee tant qu'il n'est pas rempli.")
    items = scan_items()
    print("Vendres v%s demarre. Port: %d — Dossier: %s — Annonces: %d" %
          (VERSION, CONFIG["PORT"], CONFIG["ITEMS_DIR"], len(items)))
    if not os.path.isdir(CONFIG["ITEMS_DIR"]):
        print("ATTENTION: dossier des annonces introuvable: %s" % CONFIG["ITEMS_DIR"])
    server = Server((CONFIG["HOST"], CONFIG["PORT"]), Handler)
    print("Vendres actif sur http://%s:%d/ (Ctrl+C pour arreter)" %
          ("localhost" if CONFIG["HOST"] in ("0.0.0.0", "") else CONFIG["HOST"],
           CONFIG["PORT"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
