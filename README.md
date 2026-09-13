# ValorantDeren Discord Bot

Discord-only Plattform für Bewerbungen, Aufträge, Referral und Wirtschaft (ohne Website).

## Setup

1. Python 3.11+ installieren
2. Abhängigkeiten installieren:
   - `pip install -r requirements.txt`
3. Umgebungsvariablen setzen:
   - `.env.example` nach `.env` kopieren und IDs/Token eintragen
4. Bot starten:
   - `python main.py`

## Architektur

- Einstiegspunkt: `main.py` / `bot/main.py`
- Cogs: `bot/cogs/`
- Datenbank: SQLite via `aiosqlite`
- Schema: `bot/db/schema.sql`
- Migrationen: `bot/db/migrations/` (automatisch bei Start)

## Kern-Commands

- Tickets/Bewerbung:
  - `/tickets_setup`
  - `/ticket_close`
- Aufträge:
  - `/order_create`
  - `/my_orders`
  - `/order_complete`
- Wirtschaft/Referral:
  - `/balance`
  - `/referral_create`
  - `/referral_stats`
  - `/withdraw_request`
  - `/withdraw_approve`
  - `/withdraw_reject`
  - `/transactions`
- Admin:
  - `/admin_ping`
  - `/application_decide`
  - `/admin_adjust_balance`
  - `/order_set_status`

## Audit-Logs und Booster-Meilensteine

- Optionaler Staff-Log-Channel über `STAFF_AUDIT_LOG_CHANNEL_ID`
- Sensitive Aktionen werden als Embed protokolliert
- Referral-Erstellung kann über `REFERRAL_ALLOWED_ROLE_IDS` auf bestimmte Rollen begrenzt werden
- Booster-Milestones können über `BOOSTER_COMPLETION_CHANNEL_ID` und `BOOSTER_COMPLETION_THRESHOLDS` automatisch in einen Channel gesendet werden

Beispiel:

```env
REFERRAL_ALLOWED_ROLE_IDS=123456789012345678,987654321098765432
BOOSTER_COMPLETION_CHANNEL_ID=111111111111111111
BOOSTER_COMPLETION_THRESHOLDS=10,25,50,100
```

## Hinweis

Slash-Commands werden bei gesetzter `GUILD_ID` guild-spezifisch synchronisiert, sonst global.
