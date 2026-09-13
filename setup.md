# setup.md – Ubuntu Server Setup (Docker + CI/CD) und kurzer Raw-Test

## 1) Ubuntu Server mit Docker produktiv aufsetzen

## Voraussetzungen
- Ubuntu 22.04/24.04
- Domain oder feste IP
- Discord Bot Token vorhanden
- Server-Zugriff per SSH

## 1.1 System vorbereiten
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ca-certificates curl gnupg lsb-release git
```

## 1.2 Docker + Compose installieren
```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```
Danach einmal neu einloggen.

## 1.3 Projekt deployen
```bash
mkdir -p /opt/valorant-bot
cd /opt/valorant-bot
git clone <DEIN_REPO_URL> .
```

## 1.4 Docker-Dateien anlegen

### Dockerfile
```dockerfile
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "main.py"]
```

### .dockerignore
```gitignore
.git
__pycache__
*.pyc
.env
data
```

### docker-compose.yml
```yaml
services:
  valorant-bot:
    build: .
    container_name: valorant-bot
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
```

## 1.5 .env für Produktion erstellen
```bash
cp .env.example .env
nano .env
```
Setze mindestens:
- `DISCORD_TOKEN`
- `GUILD_ID` (für schnelle Command-Syncs empfohlen)
- `DATABASE_PATH=./data/valorant_bot.db`
- Rollen- und Channel-IDs (`STAFF_ROLE_IDS`, `ADMIN_ROLE_IDS`, Ticket-/Audit-IDs)

## 1.6 Container starten
```bash
docker compose up -d --build
docker compose logs -f
```

Nützliche Befehle:
```bash
docker compose ps
docker compose restart
docker compose pull
docker compose up -d --build
```

---

## 2) CI/CD mit GitHub Actions (Build + Auto-Deploy auf Ubuntu)

## 2.1 SSH Key für Deploy-User
Auf deinem lokalen Rechner:
```bash
ssh-keygen -t ed25519 -C "valorant-bot-cicd"
```

Public Key auf Server eintragen:
```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "<PUBLIC_KEY>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

## 2.2 GitHub Secrets setzen
In deinem Repo unter **Settings → Secrets and variables → Actions**:
- `SSH_HOST` (z. B. `1.2.3.4`)
- `SSH_USER` (z. B. `ubuntu`)
- `SSH_PRIVATE_KEY` (kompletter privater Key)
- `DEPLOY_PATH` (z. B. `/opt/valorant-bot`)

## 2.3 Workflow-Datei anlegen
Datei: `.github/workflows/deploy.yml`

```yaml
name: Deploy Bot

on:
  push:
    branches: [ "main" ]

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Syntax check
        run: python -m compileall .

      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.SSH_HOST }}
          username: ${{ secrets.SSH_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            set -e
            cd ${{ secrets.DEPLOY_PATH }}
            git pull
            docker compose up -d --build
            docker image prune -f
```

Damit gilt:
- Push auf `main` -> Build/Syntax-Check -> SSH Deploy -> Container-Neustart.

---

## 3) Kurzer Raw-Test ohne Docker & ohne CI/CD (separater Test-Ordner)

Falls du schnell „nackt“ testen willst:

```bash
mkdir -p ~/valorant-bot-raw-test
cd ~/valorant-bot-raw-test
git clone <DEIN_REPO_URL> .
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python -m compileall .
python main.py
```

Stoppen mit `CTRL+C`.

Optional im Hintergrund:
```bash
nohup .venv/bin/python main.py > bot.log 2>&1 &
tail -f bot.log
```

---

## 4) Empfohlene Reihenfolge
1. Erst Raw-Test (5 Minuten)
2. Dann Docker-Setup auf `/opt/valorant-bot`
3. Danach CI/CD aktivieren
4. Zum Schluss nur noch über Git pushen/deployen






