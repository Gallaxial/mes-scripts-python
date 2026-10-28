#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini-web.py — Mini serveur web statique, en Python.

Remplace Apache quand on veut juste afficher des pages : sert les
fichiers du dossier ou il se trouve, sur le port 80, en lecture
seule. Aucune navigation (jamais de liste de dossiers), aucun
module, rien a configurer ailleurs. Completement independant
du robot FREQ / mailbot.

Utilisation :
  1. Mets tes pages et fichiers (images...) dans le MEME dossier
     que ce script.
  2. Ajuste les reglages ci-dessous si besoin.
  3. Lance lance-mini-web.bat (il se met en administrateur tout
     seul : le port 80 l'exige souvent sous Windows).

v1.2 : purge du journal au demarrage (PURGE_JOURS, defaut 30) :
log/mini-web.log ne garde que les lignes des 30 derniers jours.
Les lignes sans date lisible sont effacees.
"""

import os
import re
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ==================== VERSION ====================
# Numero de version du script (MAJEUR.MINEUR) — affiche dans la
# console au demarrage. Meme regle que le mailbot :
#   - changement MAJEUR (nouvelle fonction, changement de comportement)
#     -> MAJEUR + 1, MINEUR remis a 0  (ex. 1.0 -> 2.0)
#   - petit changement (correctif, texte, reglage)
#     -> MINEUR + 1  (ex. 1.0 -> 1.1)
VERSION = "1.2"

# ==================== REGLAGES ====================
PORT = 80                       # port d'ecoute (80 = le web normal)
PAGE_ACCUEIL = "fidonet.htm"    # page affichee pour l'adresse "/"
DOSSIER = os.path.dirname(os.path.abspath(__file__))  # dossier servi
PURGE_JOURS = 30                # jours de journal gardes a chaque demarrage
                                # (0 = pas de purge) : log/mini-web.log ne garde
                                # que les lignes des 30 derniers jours.
                                # Les lignes sans date lisible sont effacees.
# ==================================================


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


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


class ServeurWeb(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Un visiteur qui coupe sa connexion en plein transfert
        # est normal : on l'ignore au lieu d'un traceback complet.
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            return
        log("Erreur HTTP : %r" % exc)


class HandlerPage(SimpleHTTPRequestHandler):
    """Sert les fichiers du dossier, la page d'accueil pour "/",
    et rien d'autre : un dossier ne montre jamais son contenu."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOSSIER, **kwargs)

    def log_message(self, format, *args):
        # Journal des acces : dans log/mini-web.log (jamais servi,
        # voir send_head), pour garder la fenetre du .bat propre.
        try:
            os.makedirs(os.path.join(DOSSIER, "log"), exist_ok=True)
            ligne = "%s - - [%s] %s\n" % (self.address_string(),
                                          self.log_date_time_string(),
                                          format % args)
            with open(os.path.join(DOSSIER, "log", "mini-web.log"),
                      "a", encoding="utf-8") as f:
                f.write(ligne)
        except Exception:
            pass

    def send_head(self):
        # Le dossier log/ n'est jamais servi : les logs restent prives.
        if urlsplit(self.path).path == "/log" or urlsplit(self.path).path.startswith("/log/"):
            self.send_error(404)
            return None
        # L'adresse "/" affiche la page d'accueil configuree.
        if urlsplit(self.path).path in ("", "/"):
            self.path = "/" + PAGE_ACCUEIL
        return super().send_head()

    def list_directory(self, path):
        # Jamais de liste de dossiers.
        self.send_error(404)
        return None


def main():
    page = os.path.join(DOSSIER, PAGE_ACCUEIL)
    if not os.path.isfile(page):
        print("ERREUR : page d'accueil introuvable : " + page)
        print("Mets " + PAGE_ACCUEIL + " dans le meme dossier que "
              "mini-web.py, ou change PAGE_ACCUEIL dans le script.")
        sys.exit(1)
    res_purge = purger_journal(os.path.join(DOSSIER, "log", "mini-web.log"),
                               PURGE_JOURS)
    if res_purge:
        log("Purge journal : %d ligne(s) gardee(s), %d effacee(s) (%d derniers jours)"
            % (res_purge[0], res_purge[1], PURGE_JOURS))
    try:
        serveur = ServeurWeb(("0.0.0.0", PORT), HandlerPage)
    except OSError as e:
        print("ERREUR : impossible d'ouvrir le port %d (%s)" % (PORT, e))
        print("Causes frequentes : Apache encore ouvert (il garde le "
              "port 80), ou le .bat n'est pas lance en administrateur.")
        sys.exit(1)
    log("Mini serveur web v%s actif sur le port %d" % (VERSION, PORT))
    log("Page d'accueil : %s (dossier servi : %s)" % (PAGE_ACCUEIL, DOSSIER))
    log("Journal des acces : log/mini-web.log — Ctrl+C pour arreter.")
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
