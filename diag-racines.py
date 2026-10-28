"""Diagnostic racines SSL — cherche la racine SSL.com dans les
magasins Windows. Copie-colle la sortie entiere dans le chat.
"""
import os
import ssl
import tempfile

MAGASINS = ["ROOT", "CA",
            "LocalMachine\\ROOT", "LocalMachine\\CA",
            "CurrentUser\\ROOT", "CurrentUser\\CA"]


def sujet_cn(der):
    """Nom commun (CN) du sujet d'un certificat DER, ou '?'."""
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        fd, chemin = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(pem)
            info = ssl._ssl._test_decode_cert(chemin)
        finally:
            os.remove(chemin)
        for partie in info.get("subject", []):
            for cle, val in partie:
                if cle == "commonName":
                    return val
    except Exception:
        pass
    return "?"


print("Python :", __import__("sys").version.split()[0])
for magasin in MAGASINS:
    try:
        certs = ssl.enum_certificates(magasin)
    except Exception as e:
        print("%s : erreur : %s" % (magasin, e))
        continue
    noms = [sujet_cn(der) for der in certs]
    sslcom = [n for n in noms if "ssl.com" in n.lower()]
    print("%s : %d certificat(s), SSL.com trouves : %s"
          % (magasin, len(certs), sslcom if sslcom else "aucun"))
print("Fini.")
input("Appuie sur Entree pour fermer...")
