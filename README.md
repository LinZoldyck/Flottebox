# FlotteBox — gestion d'une flotte d'utilitaires en autopartage (v1)

Application web pour louer des véhicules utilitaires **sur Getaround** et **en direct** à des professionnels.
Elle est pensée dès le départ pour devenir un SaaS : chaque société a son propre espace, ses utilisateurs et ses intégrations.

| Besoin | Ce que fait la v1 |
|---|---|
| 1. Comptes, rôles, authentification | Inscription d'une société, connexion par mot de passe (bcrypt), sessions sécurisées, protection CSRF, limitation des tentatives. Rôles **Gérant / Gestionnaire / Lecture seule**. |
| 2. Contrats des locations hors plateforme | Contrat PDF généré automatiquement. Même structure que le contrat Getaround (Réservation / Informations de début / Informations de fin), plus le **logo**, les **photos avant/après**, les conditions générales modifiables et la trace de l'acceptation électronique. Exemple : [`docs/exemple-contrat.pdf`](docs/exemple-contrat.pdf). |
| Parcours locataire (cas n°1) | Code personnel envoyé **par SMS 30 min avant** la location. L'espace mobile `/r` affiche les étapes : identité et permis → acceptation du contrat → **code du cadenas Masterlock** → état des lieux de départ (kilométrage, carburant, propreté, dommages, 6 photos obligatoires et photos libres) → état des lieux de retour → code du cadenas pour rendre les clés. |
| Parcours Getaround (cas n°2) | Les réservations Getaround sont synchronisées (API Owner + webhooks). Le déverrouillage (Getaround Connect) et le contrat restent dans l'application Getaround : l'API publique ne permet pas d'ouvrir le véhicule. |
| 3. Assistant de messagerie Getaround | Les messages des locataires sont relevés toutes les 5 min. **Claude** répond seul aux questions simples (accès, horaires, carburant…) à partir de votre base de connaissances. Tout ce qui engage une décision (argent, dommages, annulation, accident, PV, litige…) vous est **transmis**, avec un double filtre : mots-clés côté serveur et décision du modèle. Trois modes : désactivé, brouillon à valider (par défaut) ou automatique. |
| 4. Contraventions → location | Déposez les avis (PDF, photo) dans l'appli ou dans un **dossier surveillé**. Les PDF texte sont lus localement ; les scans sont lus par Claude. L'appli retrouve ensuite le véhicule (plaque) et la location en cours à l'heure de l'infraction, **directe ou Getaround**. |
| 5. Désignation du conducteur / paiement des FPS | Un robot (Playwright) ouvre le site de l'ANTAI dans un vrai navigateur et **remplit chaque étape** : avis, société, conducteur. **Captcha et validation restent à vous.** |
| 6. Factures Pennylane | Depuis une location terminée : création du client, puis de la facture (location + km supplémentaires) **en brouillon** dans Pennylane, sans doublon. |

## Démarrage rapide (sur un ordinateur)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # puis éditez SECRET_KEY, BASE_URL…
python -m app.cli seed-demo        # données fictives : demo@flottebox.local / demo-flottebox
uvicorn app.main:app --reload      # http://localhost:8000
```

Avec `SMS_PROVIDER=console`, les SMS s'affichent dans les logs du serveur, ce qui permet de tester le parcours locataire sur `/r`.

## Déploiement sur Raspberry Pi (recommandé)

Il faut un Raspberry Pi 4 ou 5 (2 Go de RAM minimum) avec **Raspberry Pi OS 64 bits**.

**1. Installer Docker sur le Pi** (une seule fois) :
```bash
curl -fsSL https://get.docker.com | sudo sh     # installe Docker Engine + le plugin « docker compose »
sudo usermod -aG docker $USER                    # utiliser docker sans sudo…
newgrp docker                                    # …tout de suite (ou déconnectez-vous / reconnectez-vous)
docker run --rm hello-world                      # vérification
```
Le démon Docker démarre automatiquement au boot (service systemd `docker`).

**2. Installer l'application** :
```bash
git clone https://github.com/LinZoldyck/testrespo.git flottebox && cd flottebox
cp .env.example .env && nano .env              # SECRET_KEY, BASE_URL, SMS, ANTHROPIC_API_KEY…
docker compose up -d --build                    # construit l'image sur le Pi (≈ 5 min la 1re fois)
docker compose exec -u flotte app python -m app.cli create-owner --company "Ma Société" --email moi@exemple.fr --name "Prénom Nom"
```
L'application répond sur `http://<ip-du-pi>:8000`. Le conteneur redémarre tout seul après une coupure ou un reboot.

Commandes utiles :
```bash
docker compose logs -f app                       # journaux (dont les SMS en mode « console »)
git pull && docker compose up -d --build         # mise à jour
docker compose exec -u flotte app python -m app.cli scan-fines
```
Lancez toujours les commandes `exec` avec `-u flotte`, sinon les fichiers créés appartiendront à root.

L'application doit être joignable **en HTTPS depuis Internet**. C'est nécessaire pour trois raisons : les locataires ouvrent le lien du SMS sur leur téléphone, Getaround envoie ses webhooks, et l'appareil photo du navigateur l'exige. Deux options :

* **Cloudflare Tunnel** (aucun port à ouvrir) : créez un tunnel vers `http://app:8000`, mettez `CLOUDFLARE_TUNNEL_TOKEN=…` dans `.env`, puis lancez `docker compose --profile tunnel up -d`.
* **Caddy** (certificat automatique) : redirigez les ports 80/443 de la box vers le Pi, modifiez `deploy/Caddyfile`, puis lancez `docker compose --profile https up -d`.

Toutes les données (base SQLite, photos, contrats, avis) sont dans `./data` : **sauvegardez ce dossier** (par exemple `rsync` ou `restic` chaque nuit).
La CI GitHub publie aussi une image `ghcr.io/linzoldyck/testrespo:latest` (amd64 + arm64) à chaque push sur `main`.

Sans Docker : créez un venv dans `/home/pi/flottebox`, installez `requirements.txt` et `fonts-dejavu-core`, puis utilisez `deploy/flottebox.service` (systemd).

> **Pourquoi pas GitHub Pages ?** GitHub Pages ne sert que des pages statiques. Or l'application doit garder des secrets (clés Getaround, Pennylane, Claude), envoyer des SMS, tourner en tâche de fond (codes, synchronisation, PV) et stocker les photos. Cela demande un serveur : le Raspberry Pi convient très bien.

## Configuration dans l'application

1. **Paramètres** : informations de la société, logo, conditions générales du contrat, base de connaissances de l'assistant, jetons Getaround et Pennylane, jeton du robot.
2. **Véhicules** : plaque, code du cadenas, tarifs. L'ID Getaround est associé automatiquement par la plaque lors de la synchronisation.
3. **Clients** puis **Locations > + Location directe** : le prix, les km inclus et le dépôt sont calculés à partir du tarif du véhicule.

### Getaround
* L'API Owner s'obtient sur demande auprès de Getaround (comptes pro) : <https://api-docs.getaround.com>.
* Demandez à Getaround d'activer les webhooks vers `https://<votre-domaine>/webhooks/getaround/<id-société>`. L'URL exacte est affichée dans Paramètres.
* Les noms de champs des réponses sont lus de façon tolérante et la réponse brute est conservée. **Vérifiez la synchronisation sur vos premières locations réelles**, car certains champs (identité du conducteur notamment) dépendent de vos droits API.

### Pennylane
Créez un jeton API (scopes `customers:all` et `customer_invoices:all`). Les factures sont créées **en brouillon** : vous les finalisez dans Pennylane. Le taux de TVA par défaut est `FR_200` (20 %).

### Claude
Renseignez `ANTHROPIC_API_KEY` dans `.env`. Modèle par défaut : `claude-opus-5-5` en effort `low`, réglable avec `CLAUDE_MODEL` et `CLAUDE_EFFORT`.
Le *fallback* serveur est activé : si une requête est refusée par un filtre de sécurité, l'API la rejoue sur un autre modèle.
Sans clé, l'appli fonctionne quand même : tous les messages vous sont transmis, et seuls les avis en PDF texte sont lus automatiquement.

### Contraventions : dossier surveillé
Déposez les fichiers dans `data/contraventions_a_traiter/<id-société>/` (partage Samba, Syncthing, scanner réseau…). Ils sont analysés toutes les 5 minutes puis déplacés dans `traites/`.

### Robot de désignation ANTAI (sur votre ordinateur)
```bash
pip install -r robot/requirements.txt && playwright install chromium
export FLOTTEBOX_URL=https://flotte.mondomaine.fr FLOTTEBOX_TOKEN=fb_…   # jeton : Paramètres > Jeton du robot
python robot/antai_designation.py --list
python robot/antai_designation.py --fine 12
```
Les étapes et les libellés des champs sont décrits dans `robot/antai_steps.json`. Si un champ n'est pas trouvé, le robot vous donne la valeur à saisir et attend. **Vérifiez les libellés lors de la première utilisation** : le site de l'ANTAI peut évoluer.

### Forfaits post-stationnement (FPS)
Un FPS ne se désigne pas : le titulaire de la carte grise le paie, puis le refacture au locataire. Quand un FPS est déposé, l'application :
* lit le numéro de l'avis de paiement (par groupes), la **clé**, le montant et la date d'envoi, et calcule la date limite de recours (1 mois) et de paiement (3 mois, avant majoration) ;
* affiche une fiche **« Payer ce FPS »** avec boutons de copie et lien vers le site officiel ;
* propose un robot de pré-remplissage du paiement, qui ne saisit **jamais** la carte bancaire :
  ```bash
  python robot/fps_payment.py --list      # FPS restant à payer
  python robot/fps_payment.py --fine 12   # remplit numéro d'avis + clé, vérifie le montant, puis vous laisse payer
  ```
  Les étapes et les libellés sont dans `robot/fps_payment_steps.json`. Le numéro se remplit dans un seul champ ou case par case. Vérifiez les libellés lors du premier paiement.
* une fois le FPS payé, permet de le **refacturer** au locataire (Pennylane, brouillon), frais de gestion compris si vous le souhaitez.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt && pytest -q
```
Les tests couvrent : rôles et CSRF, parcours locataire complet (code SMS, identité, contrat, états des lieux avec photos, PDF), double réservation, lecture et rapprochement des PV (direct et Getaround), API du robot, garde-fous de l'assistant, synchronisation Getaround, webhook signé et facturation Pennylane (APIs simulées).

## Architecture

```
app/
  main.py, config.py, db.py, models.py, security.py, web.py, cli.py
  routers/   auth (comptes, rôles) · fleet (véhicules, clients, locations, contrats, factures)
             renter (espace locataire /r) · ops (PV, messagerie, paramètres) · api (robot, webhooks)
  services/  contracts (PDF) · fines (lecture + rapprochement) · ai_assistant (Claude) · getaround
             pennylane · sms (Twilio/Brevo) · storage (photos) · jobs (planificateur)
  templates/ HTML (Jinja2), static/ CSS — sans étape de build
robot/       désignation ANTAI (Playwright)
deploy/      Caddyfile, service systemd
```
Stack : FastAPI, SQLAlchemy et SQLite (un fichier, idéal sur un Pi), fpdf2, APScheduler, SDK Anthropic.
Pour passer au SaaS multi-clients, il faudra : PostgreSQL (`DATABASE_URL`), chiffrement des jetons en base, migrations (Alembic), un worker séparé pour les tâches, des e-mails transactionnels et de la facturation d'abonnement.

## Limites connues de la v1
* Ouverture du véhicule via Getaround : impossible par l'API publique, elle se fait dans l'application Getaround.
* La signature du contrat est une acceptation électronique simple (case à cocher, nom, horodatage, IP, code SMS), pas une signature qualifiée (Yousign…).
* Les formats exacts des API Getaround et Pennylane et les libellés du site ANTAI sont à valider sur vos comptes réels. Les points concernés sont isolés dans `services/getaround.py`, `services/pennylane.py` et `robot/antai_steps.json`.
