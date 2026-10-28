"""Diagnostic SSL Gutenberg — a lancer UNE FOIS sur le PC Windows.

Ne telecharge rien : il teste juste si Python arrive a parler a
gutenberg.org avec le contexte enrichi (magasin Windows) ou non.
Copie-colle la sortie entiere dans le chat.
"""
import ssl
import sys
import urllib.request


def contexte_enrichi():
    """Meme logique que fetcher.py : defaut + racines Windows CA/ROOT."""
    ctx = ssl.create_default_context()
    charges = {}
    for magasin in ("CA", "ROOT"):
        try:
            certs = ssl.enum_certificates(magasin)
            charges[magasin] = len(certs)
            for der in certs:
                try:
                    ctx.load_verify_locations(
                        cadata=ssl.DER_cert_to_PEM_cert(der))
                except Exception:
                    pass
        except Exception as e:
            charges[magasin] = "erreur : %s" % e
    return ctx, charges


def test(url, ctx, nom):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DiagSSL/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=ctx) as rep:
            rep.read(1)
        print("%s : OK (%d)" % (nom, rep.status))
    except Exception as e:
        print("%s : ECHEC : %s" % (nom, e))


print("Python :", sys.version.split()[0])
ctx, charges = contexte_enrichi()
print("Certificats Windows lus :", charges)
test("https://gutendex.com/books?search=test",
     ssl.create_default_context(), "Gutendex (defaut)")
test("https://www.gutenberg.org/",
     ssl.create_default_context(), "gutenberg.org (defaut)")
test("https://www.gutenberg.org/", ctx, "gutenberg.org (enrichi Windows)")
print("Fini.")
input("Appuie sur Entree pour fermer...")
