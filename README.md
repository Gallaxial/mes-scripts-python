# mes-scripts-python

Scripts Python en bibliothèque standard seulement, pour Windows.
Pas de dépendance externe, pas de PHP, pas d'Apache : chaque script
se lance avec `python script.py` (ou par un `.bat` sous Windows).

## Contenu

- `mailbot-ftp.py` (v2.4) — serveur de demandes de fichiers par
  courriel, avec jetons par demandeur et réseau FREQ fédéré
  (ANNOUNCE, RESEAU, LISTE-RESEAU, CHERCHE-RESEAU).
- `dumpbox.py` (v1.11) — dépôt d'archives par page web
  (.zip/.rar/.7z seulement), avec limite de vitesse partagée.
- `mini-web.py` (v1.2) — petit serveur web statique, port 80.
- `stats.py` (v1.4) — pages HTML de statistiques à partir des
  journaux des serveurs, avec détail par code de statut.
- `fetcher.py` (v1.17) — téléchargement de documents libres
  (Internet Archive, arXiv, Gutenberg, Semantic Scholar, OpenAlex,
  NASA NTRS, liste d'URLs), avec suivi de progression.
- `diag-ssl.py`, `diag-chaine.py`, `diag-racines.py` — diagnostics
  SSL sous Windows (chaîne de certificats, magasins de racines).
- `vendres.py` (v3.6) — petites annonces, avec vignettes générées
  par ffmpeg, formulaire de vente et tableau de bord admin.
- `simul-freq.py` (v1.2) — simulation d'un courriel demandeur
  FREQ, pour tester un serveur (dépose un `.msg` dans la file
  locale MDaemon, tests seulement).

## Avant de lancer

Les valeurs par défaut sont des exemples génériques : adaptez
dossier racine, port, adresse courriel et mots de passe à votre
installation. Les mots de passe ne sont jamais dans ce dépôt :
ils vivent dans les fichiers `.conf` / `.json` à côté de chaque
script, qui ne sont pas inclus ici. Remplissez-les chez vous.

## Licence

Utilisation libre, sans garantie. Si ces scripts vous servent,
une étoile sur ce dépôt aide à le faire connaître.
