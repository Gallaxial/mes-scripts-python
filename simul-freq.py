#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
simul-freq.py — Simule un courriel de demandeur FREQ, en Python.

Formulaire texte : le script pose 4 questions (DE, A, SUJET,
message) et ecrit un fichier .msg directement dans la file locale
de MDaemon (C:\MDaemon\Queues\Local par defaut). MDaemon le livrera
dans la boite du destinataire, et le robot mailbot le lira et
repondra a l'adresse de l'expediteur.

A chaque question, Entree = garder la valeur proposee (les valeurs
par defaut viennent de simul-freq.conf, a cote du script).
Le script ne touche a rien d'autre : il ne lit ni ne modifie les
fichiers du mailbot, il ecrit juste un fichier .msg.

Format du fichier : calque sur un vrai fichier de file MDaemon
(verifie le 2026-10-26 sur un courriel webmail) — en-tetes de
routage X-MDaemon-Deliver-To, X-Rcpt-To, X-MDRcpt-To,
X-Envelope-From, plus les en-tetes normaux (From, To, Subject,
Date). Ecriture atomique : le fichier est d'abord ecrit en .tmp
puis renomme en .msg, pour que MDaemon ne voie jamais un fichier
incomplet.

Exemple : DE = demandeur@exemple.com, A = freq@exemple.com,
SUJET = list-network -> le serveur FREQ1 envoie sa liste au
demandeur et relaie la demande a ses pairs (Fidonet), qui envoient
chacun leur liste directement au demandeur.

v1.1 : formulaire texte interactif a la place des valeurs figees
du config pour DE, A, SUJET et le message. Le config garde
DOSSIER_QUEUE et les valeurs proposees par defaut.
v1.2 : BODY dans le config (message propose par defaut) ; un \n
dans BODY (ou dans le message tape au formulaire) devient un vrai
saut de ligne dans le .msg, car une ligne du config et une reponse
du formulaire ne peuvent pas contenir de saut de ligne reel.
"""

# ==================== VERSION ====================
# Numero de version du script (MAJEUR.MINEUR) — affiche dans la
# console au demarrage. Meme regle que les autres serveurs :
#   - changement MAJEUR (nouvelle fonction, changement de comportement)
#     -> MAJEUR + 1, MINEUR remis a 0  (ex. 1.0 -> 2.0)
#   - petit changement (correctif, texte, reglage)
#     -> MINEUR + 1  (ex. 1.0 -> 1.1)
# VERSION = "1.2"
VERSION = "1.2"

import os
import time
from email.utils import formatdate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(SCRIPT_DIR, "simul-freq.conf")

DEFAUTS = {
    "DOSSIER_QUEUE": "C:/MDaemon/Queues/Local",
    "DE": "demandeur@exemple.com",
    "A": "freq@exemple.com",
    "SUJET": "list-network",
    "BODY": "",
}

MODELE_CONFIG = """# simul-freq.conf — Simulation d'un courriel de demandeur FREQ.
# Une ligne = un reglage NOM = valeur. Les lignes # sont ignorees.
# DOSSIER_QUEUE : la file locale de MDaemon (C:/MDaemon/Queues/Local).
#   Si MDaemon ignore le fichier dans Local, essaie la file d'entree :
#   C:/MDaemon/Queues/Inbound (meme format, une ligne a changer).
# DE / A / SUJET : valeurs proposees par defaut dans le formulaire
#   (Entree = garder la valeur proposee).
# BODY : message propose par defaut. Une ligne du config ne peut pas
#   contenir de vrai saut de ligne : on ecrit \n entre les lignes,
#   le script le transforme en saut de ligne dans le .msg.
#   Exemple : BODY = Adresse: exemple.com:8084\nCle: ta-cle-reseau
DOSSIER_QUEUE = C:/MDaemon/Queues/Local
DE = demandeur@exemple.com
A = freq@exemple.com
SUJET = list-network
BODY =
"""


def lire_config():
    """Lit simul-freq.conf, ou le cree avec les valeurs par defaut."""
    if not os.path.exists(CONFIG):
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                f.write(MODELE_CONFIG)
        except OSError as exc:
            print("Impossible de creer %s : %s" % (CONFIG, exc))
            return None
        print("Fichier %s cree avec les valeurs par defaut." % CONFIG)
        print("Modifie-le si besoin, puis relance le script.")
        return None
    cfg = dict(DEFAUTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne or ligne.startswith("#") or "=" not in ligne:
                    continue
                nom, valeur = ligne.split("=", 1)
                cfg[nom.strip()] = valeur.strip()
    except OSError as exc:
        print("Impossible de lire %s : %s" % (CONFIG, exc))
        return None
    return cfg


def demander(question, defaut):
    """Une question du formulaire. Entree vide = valeur par defaut."""
    try:
        reponse = input("%s [%s] : " % (question, defaut)).strip()
    except EOFError:
        reponse = ""
    return reponse or defaut


def decoder_lignes(texte):
    """Transforme chaque \n tape en vrai saut de ligne."""
    return texte.replace("\\n", "\n")


def construire_message(de, a, sujet, message):
    """Le contenu du .msg, avec les en-tetes de routage MDaemon."""
    horodatage = time.strftime("%Y%m%d%H%M%S", time.localtime())
    transfert = "7bit" if all(ord(c) < 128 for c in message) else "8bit"
    lignes = [
        "X-MDaemon-Deliver-To: %s" % a,
        "X-Rcpt-To: %s" % a,
        "X-MDRcpt-To: %s" % a,
        "X-Envelope-From: %s" % de,
        "X-Return-Path: %s" % de,
        "Return-path: <%s>" % de,
        "Date: %s" % formatdate(localtime=True),
        "From: %s" % de,
        "To: %s" % a,
        "Subject: %s" % sujet,
        "MIME-Version: 1.0",
        "Message-ID: <simul-%s@exemple.com>" % horodatage,
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: %s" % transfert,
        "",
    ]
    lignes.extend(message.split("\n"))
    lignes.append("")
    return "\r\n".join(lignes)


def chemin_unique(dossier, base):
    """Evite d'ecraser un fichier existant : ajoute (2), (3)..."""
    nom, ext = os.path.splitext(base)
    candidat = base
    numero = 2
    while os.path.exists(os.path.join(dossier, candidat)):
        candidat = "%s (%d)%s" % (nom, numero, ext)
        numero += 1
    return os.path.join(dossier, candidat)


def main():
    cfg = lire_config()
    if cfg is None:
        return
    print("SimulFREQ v%s — formulaire (Entree = valeur proposee)." % VERSION)
    try:
        de = demander("DE (expediteur)", cfg["DE"])
        a = demander("A (destinataire)", cfg["A"])
        sujet = demander("SUJET", cfg["SUJET"])
        message = demander("Message (Entree = BODY du config ou message auto)",
                            cfg.get("BODY", ""))
    except KeyboardInterrupt:
        print("\nAnnule.")
        return
    if message:
        message = decoder_lignes(message)
    else:
        message = "Courriel de simulation FREQ.\nDemande : %s" % sujet
    dossier = cfg["DOSSIER_QUEUE"]
    try:
        os.makedirs(dossier, exist_ok=True)
    except OSError as exc:
        print("Impossible d'utiliser %s : %s" % (dossier, exc))
        return
    base = "simul-%s.msg" % time.strftime("%Y%m%d-%H%M%S", time.localtime())
    final = chemin_unique(dossier, base)
    tmp = final + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(construire_message(de, a, sujet, message))
        os.replace(tmp, final)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("ERREUR d'ecriture dans %s : %s" % (dossier, exc))
        return
    print("Fichier ecrit :")
    print("  %s" % final)
    print("  DE : %s" % de)
    print("  A : %s" % a)
    print("  Sujet : %s" % sujet)
    print("MDaemon doit le livrer dans la boite du destinataire, puis")
    print("le robot le lira a sa prochaine verification (1-2 minutes).")
    print("Si le fichier reste dans la file plus de 2 minutes, verifie")
    print("que MDaemon tourne, et regarde le dossier BadMsgs.")


if __name__ == "__main__":
    main()
