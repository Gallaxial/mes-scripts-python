"""Diagnostic chaine Gutenberg — montre QUI a emis le certificat que
Python voit pour gutenberg.org. Ne telecharge rien et ne fait
confiance a rien : il se connecte juste pour lire le certificat
presente, puis il s'arrete.
Copie-colle la sortie entiere dans le chat.
"""
import socket
import ssl


def essai(hote, ctx, nom):
    try:
        with socket.create_connection((hote, 443), timeout=30) as sock:
            with ctx.wrap_socket(sock, server_hostname=hote) as tls:
                print("%s : OK (%s)" % (nom, tls.version()))
    except Exception as e:
        print("%s : ECHEC : %s" % (nom, e))


def chaine(hote):
    print("--- Certificat presente par %s ---" % hote)
    try:
        ctx = ssl._create_unverified_context()
        with socket.create_connection((hote, 443), timeout=30) as sock:
            with ctx.wrap_socket(sock, server_hostname=hote) as tls:
                cert = tls.getpeercert()
                for cle in ("subject", "issuer", "serialNumber",
                            "notBefore", "notAfter"):
                    print("  %s : %s" % (cle, cert.get(cle)))
    except Exception as e:
        print("  lecture impossible :", e)


print("Controle (doit passer si le SSL de Python est sain) :")
essai("www.google.com", ssl.create_default_context(), "google.com (defaut)")
print()
chaine("www.gutenberg.org")
print("Fini.")
input("Appuie sur Entree pour fermer...")
