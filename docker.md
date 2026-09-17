# Rankify: Debian, Docker und CI/CD

Diese Anleitung richtet zwei vollständig getrennte Discord-Bots auf einem Debian-Server ein:

- `development` branch -> Development-Bot und `rankify-development` Container
- `main` branch -> Release-Bot und `rankify-release` Container

Beide Container verwenden eigene Discord-Tokens, eigene Datenbanken, eigene `.env`-Dateien und eigene Docker-Compose-Projekte. Ein Fehler oder Test in Development beeinflusst den Release-Bot nicht.

## 1. Voraussetzungen

### Lokal

- GitHub-Repository mit den Branches `main` und `development`
- Zwei Discord-Bot-Anwendungen und Tokens:
  - Development-Bot für Tests
  - Release-Bot für den produktiven Server
- GitHub Actions im Repository aktiviert
- SSH-Zugang zum Debian-Server

### Debian-Server

- Debian 12 empfohlen
- mindestens 1 GB RAM
- Domain ist für den Bot nicht erforderlich
- ein Benutzer mit `sudo`-Rechten

## 2. Branch-Modell

`main` ist die produktive Version. `development` ist die Testversion.

```text
feature/my-change
        |
        v
 development  -> Development-Bot
        |
        v
 main         -> Release-Bot
```

Arbeitsablauf:

```bash
git checkout development
git pull origin development
# Änderungen entwickeln und testen
git add .
git commit -m "Test new feature"
git push origin development
```

Wenn Development erfolgreich getestet wurde:

```bash
git checkout main
git pull origin main
git merge --no-ff development -m "Release tested changes"
git push origin main
```

Ein Push auf `development` deployt nur den Development-Container. Ein Push auf `main` deployt nur den Release-Container.

## 3. Debian vorbereiten

Per SSH verbinden:

```bash
ssh admin@SERVER_IP
```

System aktualisieren und Grundpakete installieren:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y ca-certificates curl git gnupg ufw
```

Docker aus dem offiziellen Docker-Repository installieren:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $CODENAME stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Danach SSH-Verbindung beenden und neu aufbauen, damit die Docker-Gruppenmitgliedschaft aktiv wird:

```bash
exit
ssh admin@SERVER_IP
docker --version
docker compose version
```

Firewall einrichten. Discord-Bots benötigen keine offenen eingehenden Ports:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

## 4. Deploy-Verzeichnisse anlegen

Die beiden Deployments liegen bewusst in getrennten Verzeichnissen:

```bash
sudo mkdir -p /opt/rankify-development/data
sudo mkdir -p /opt/rankify-release/data
sudo chown -R "$USER":"$USER" /opt/rankify-development /opt/rankify-release
```

Repository klonen:

```bash
git clone --branch development --single-branch YOUR_REPOSITORY_URL /opt/rankify-development
git clone --branch main --single-branch YOUR_REPOSITORY_URL /opt/rankify-release
```

Die Platzhalterwerte `YOUR_REPOSITORY_URL` und `SERVER_IP` durch die echten Werte ersetzen.

## 5. Docker-Dateien

### 5.1 `Dockerfile`

Im Repository eine Datei `Dockerfile` anlegen:

```dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 botuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY main.py ./main.py

RUN mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
```

Der Container startet den vorhandenen Einstiegspunkt `main.py`. Die Datenbank bleibt außerhalb des Images im gemounteten `data`-Verzeichnis erhalten.

### 5.2 `.dockerignore`

```gitignore
.git
.github
.idea
.venv
__pycache__
*.py[cod]
.pytest_cache
.mypy_cache
.coverage
htmlcov
.env
.env.*
data
*.db
*.sqlite
*.sqlite3
```

`.env` und Datenbanken dürfen nicht in das Docker-Image gelangen.

### 5.3 `docker-compose.yml`

Diese Compose-Datei kann auf beiden Branches gleich sein. Die unterschiedlichen Werte kommen aus den jeweiligen `.env`-Dateien.

```yaml
services:
  rankify-bot:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    env_file:
      - .env
    environment:
      DATABASE_PATH: /app/data/valorant_bot.db
    volumes:
      - ./data:/app/data
    init: true
    stop_grace_period: 30s
```

Der Compose-Projektname wird beim Start mit `-p` gesetzt. Deshalb können beide Deployments denselben Service-Namen verwenden, ohne sich zu überschreiben.

## 6. Environment-Dateien

### Development

Datei `/opt/rankify-development/.env` erstellen:

```env
DISCORD_TOKEN=DEVELOPMENT_BOT_TOKEN
GUILD_ID=DEVELOPMENT_TEST_SERVER_ID
DATABASE_PATH=/app/data/valorant_bot.db

STAFF_ROLE_IDS=DEVELOPMENT_STAFF_ROLE_ID
ADMIN_ROLE_IDS=DEVELOPMENT_ADMIN_ROLE_ID
REFERRAL_ALLOWED_ROLE_IDS=DEVELOPMENT_REFERRAL_ROLE_ID

SUPPORT_TICKET_CATEGORY_ID=DEVELOPMENT_SUPPORT_CATEGORY_ID
PAYOUT_TICKET_CATEGORY_ID=DEVELOPMENT_PAYOUT_CATEGORY_ID
APPLICATION_TICKET_CATEGORY_ID=DEVELOPMENT_APPLICATION_CATEGORY_ID
ARCHIVED_TICKET_CATEGORY_ID=DEVELOPMENT_ARCHIVE_CATEGORY_ID
TICKET_TRANSCRIPT_CHANNEL_ID=DEVELOPMENT_TRANSCRIPT_CHANNEL_ID
SUGGESTION_CHANNEL_ID=DEVELOPMENT_SUGGESTION_CHANNEL_ID
REFERRAL_EARNING_CHANNEL_ID=DEVELOPMENT_REFERRAL_LOG_CHANNEL_ID
STAFF_AUDIT_LOG_CHANNEL_ID=DEVELOPMENT_AUDIT_CHANNEL_ID
```

### Release

Datei `/opt/rankify-release/.env` erstellen:

```env
DISCORD_TOKEN=RELEASE_BOT_TOKEN
GUILD_ID=PRODUCTION_SERVER_ID
DATABASE_PATH=/app/data/valorant_bot.db

STAFF_ROLE_IDS=PRODUCTION_STAFF_ROLE_ID
ADMIN_ROLE_IDS=PRODUCTION_ADMIN_ROLE_ID
REFERRAL_ALLOWED_ROLE_IDS=PRODUCTION_REFERRAL_ROLE_ID

SUPPORT_TICKET_CATEGORY_ID=PRODUCTION_SUPPORT_CATEGORY_ID
PAYOUT_TICKET_CATEGORY_ID=PRODUCTION_PAYOUT_CATEGORY_ID
APPLICATION_TICKET_CATEGORY_ID=PRODUCTION_APPLICATION_CATEGORY_ID
ARCHIVED_TICKET_CATEGORY_ID=PRODUCTION_ARCHIVE_CATEGORY_ID
TICKET_TRANSCRIPT_CHANNEL_ID=PRODUCTION_TRANSCRIPT_CHANNEL_ID
SUGGESTION_CHANNEL_ID=PRODUCTION_SUGGESTION_CHANNEL_ID
REFERRAL_EARNING_CHANNEL_ID=PRODUCTION_REFERRAL_LOG_CHANNEL_ID
STAFF_AUDIT_LOG_CHANNEL_ID=PRODUCTION_AUDIT_CHANNEL_ID
```

Dateiberechtigungen schützen die Tokens:

```bash
chmod 600 /opt/rankify-development/.env /opt/rankify-release/.env
```

Alle weiteren optionalen Variablen können aus `.env.example` übernommen werden. Discord-Tokens niemals committen oder in GitHub-Workflow-Dateien schreiben.

## 7. Erstes manuelles Deployment

Development starten:

```bash
cd /opt/rankify-development
docker compose -p rankify-development up -d --build
docker compose -p rankify-development ps
docker compose -p rankify-development logs -f --tail=100
```

Release starten:

```bash
cd /opt/rankify-release
docker compose -p rankify-release up -d --build
docker compose -p rankify-release ps
docker compose -p rankify-release logs -f --tail=100
```

Beide Container prüfen:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Erwartete Container:

```text
rankify-development-rankify-bot-1
rankify-release-rankify-bot-1
```

Die beiden Bots müssen in Discord unterschiedliche Tokens verwenden. Ein Token darf nicht gleichzeitig in beiden Containern laufen.

## 8. GitHub Actions CI/CD

Für CI/CD werden zwei Workflows angelegt. Beide prüfen Python und Docker. Danach verbindet sich der jeweilige Workflow per SSH mit dem Debian-Server, aktualisiert nur seinen Branch und startet nur seinen Container.

### 8.1 GitHub Secrets

Im Repository unter **Settings -> Secrets and variables -> Actions** anlegen:

```text
DEPLOY_HOST       Server-IP oder Hostname
DEPLOY_USER       Debian-Benutzer
DEPLOY_SSH_KEY    privater SSH-Key für den Deploy-Benutzer
DEPLOY_KNOWN_HOSTS Ausgabe von ssh-keyscan -H SERVER_IP
DEV_DEPLOY_PATH   /opt/rankify-development
PROD_DEPLOY_PATH  /opt/rankify-release
```

SSH-Key erzeugen:

```bash
ssh-keygen -t ed25519 -C "rankify-github-actions" -f ~/.ssh/rankify_actions
```

Public Key auf dem Server installieren:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cat ~/.ssh/rankify_actions.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Known Hosts lokal erzeugen und den vollständigen Inhalt als Secret `DEPLOY_KNOWN_HOSTS` speichern:

```bash
ssh-keyscan -H SERVER_IP
```

Den privaten Schlüssel `~/.ssh/rankify_actions` ausschließlich als GitHub Secret hinterlegen.

### 8.2 CI-Workflow für `development`

Datei `.github/workflows/deploy-development.yml`:

```yaml
name: Deploy Development Bot

on:
  push:
    branches:
      - development
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: rankify-development
  cancel-in-progress: true

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest
    environment: development

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install dependencies
        run: python -m pip install -r requirements.txt

      - name: Compile Python files
        run: python -m compileall -q bot main.py

      - name: Build Docker image
        run: docker build --tag rankify-development:test .

      - name: Configure SSH
        shell: bash
        env:
          SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
        run: |
          install -m 700 -d ~/.ssh
          printf '%s\n' "$SSH_KEY" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts
          chmod 600 ~/.ssh/known_hosts

      - name: Deploy development container
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.DEV_DEPLOY_PATH }}
        run: |
          ssh "$DEPLOY_USER@$DEPLOY_HOST" <<EOF
            set -eu
            cd "$DEPLOY_PATH"
            git fetch origin development
            git checkout development
            git reset --hard origin/development
            docker compose -p rankify-development up -d --build --remove-orphans
            docker image prune -f
          EOF
```

### 8.3 CI-Workflow für `main`

Datei `.github/workflows/deploy-release.yml`:

```yaml
name: Deploy Release Bot

on:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: rankify-release
  cancel-in-progress: false

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest
    environment: production

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install dependencies
        run: python -m pip install -r requirements.txt

      - name: Compile Python files
        run: python -m compileall -q bot main.py

      - name: Build Docker image
        run: docker build --tag rankify-release:test .

      - name: Configure SSH
        shell: bash
        env:
          SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
        run: |
          install -m 700 -d ~/.ssh
          printf '%s\n' "$SSH_KEY" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts
          chmod 600 ~/.ssh/known_hosts

      - name: Deploy release container
        env:
          DEPLOY_HOST: ${{ secrets.DEPLOY_HOST }}
          DEPLOY_USER: ${{ secrets.DEPLOY_USER }}
          DEPLOY_PATH: ${{ secrets.PROD_DEPLOY_PATH }}
        run: |
          ssh "$DEPLOY_USER@$DEPLOY_HOST" <<EOF
            set -eu
            cd "$DEPLOY_PATH"
            git fetch origin main
            git checkout main
            git reset --hard origin/main
            docker compose -p rankify-release up -d --build --remove-orphans
            docker image prune -f
          EOF
```

## 9. Server-Deploy einmal testen

Vor dem ersten Push die SSH-Verbindung und beide Compose-Projekte manuell testen:

```bash
ssh -i ~/.ssh/rankify_actions DEPLOY_USER@SERVER_IP 'docker version'

ssh DEPLOY_USER@SERVER_IP 'cd /opt/rankify-development && git status --short'
ssh DEPLOY_USER@SERVER_IP 'cd /opt/rankify-release && git status --short'
```

Danach zuerst Development deployen:

```bash
git checkout development
git push origin development
```

Logs ansehen:

```bash
ssh DEPLOY_USER@SERVER_IP \
  'docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml logs -f --tail=100'
```

Erst nach erfolgreichem Test nach `main` mergen:

```bash
git checkout main
git merge --no-ff development -m "Release tested development changes"
git push origin main
```

Release-Logs ansehen:

```bash
ssh DEPLOY_USER@SERVER_IP \
  'docker compose -p rankify-release -f /opt/rankify-release/docker-compose.yml logs -f --tail=100'
```

## 10. Betrieb und Wartung

Status:

```bash
docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml ps
docker compose -p rankify-release -f /opt/rankify-release/docker-compose.yml ps
```

Logs:

```bash
docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml logs -f --tail=200
docker compose -p rankify-release -f /opt/rankify-release/docker-compose.yml logs -f --tail=200
```

Neustart:

```bash
docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml restart
docker compose -p rankify-release -f /opt/rankify-release/docker-compose.yml restart
```

Stoppen:

```bash
docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml down
docker compose -p rankify-release -f /opt/rankify-release/docker-compose.yml down
```

Datenbank-Backups:

```bash
sudo install -d -m 700 /var/backups/rankify
sudo tar -czf /var/backups/rankify/development-$(date +%F).tar.gz \
  -C /opt/rankify-development data
sudo tar -czf /var/backups/rankify/release-$(date +%F).tar.gz \
  -C /opt/rankify-release data
```

Alte Docker-Images entfernen:

```bash
docker image prune -f
```

## 11. Troubleshooting

### Container startet nicht

```bash
docker compose -p rankify-development -f /opt/rankify-development/docker-compose.yml logs --tail=200
```

Typische Ursachen:

- `DISCORD_TOKEN` fehlt oder ist ungültig
- `.env` hat falsche Berechtigungen oder falsche IDs
- Bot ist nicht auf dem erwarteten Discord-Server
- Discord-Bot hat notwendige Rechte nicht
- Datenbankpfad zeigt nicht auf `/app/data/valorant_bot.db`

### Slash-Commands fehlen

- Prüfen, ob `GUILD_ID` gesetzt ist.
- Bot neu starten.
- Prüfen, ob der Bot mit dem Scope `applications.commands` eingeladen wurde.
- Logs auf `Synced ... guild slash command(s)` prüfen.

### Development und Release überschreiben sich

Prüfen:

```bash
docker ps --format '{{.Names}}'
docker compose ls
```

Es müssen zwei Compose-Projekte existieren:

```text
rankify-development
rankify-release
```

Niemals beide Branches im selben Verzeichnis deployen und niemals denselben `.env`- oder `data`-Pfad verwenden.

### Deployment soll zurückgerollt werden

Auf dem Server im betroffenen Verzeichnis:

```bash
git log --oneline -5
git checkout main
git reset --hard origin/main~1
docker compose -p rankify-release up -d --build
```

Besser ist ein Revert im GitHub-Repository:

```bash
git revert COMMIT_SHA
git push origin main
```

So bleibt die Historie nachvollziehbar und CI/CD deployt den Revert automatisch.

## 12. Sicherheitsregeln

- Tokens ausschließlich in `.env` oder GitHub Secrets speichern.
- `.env`, `data/` und Datenbanken niemals committen.
- `DEPLOY_KNOWN_HOSTS` verwenden; SSH-Host-Key-Prüfung nicht deaktivieren.
- Den Docker-Socket nicht ins Bot-Container mounten.
- Nur SSH am Server öffnen; der Bot braucht keine offenen HTTP-Ports.
- Development und Release immer mit unterschiedlichen Discord-Bot-Tokens betreiben.
- Vor dem Merge in `main` den Development-Bot vollständig testen.
