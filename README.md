# Rankify Discord Bot

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
  - `/set_archived_ticket_category`
  - `/set_ticket_transcript_channel`
  - `/set_referral_earning_channel`
  - `/set_suggestion_channel`
  - `/suggestions_setup`
  - `/application_decide`
  - `/admin_adjust_balance`
  - `/order_set_status`

## Audit-Logs und Booster-Meilensteine

- Optionaler Staff-Log-Channel über `STAFF_AUDIT_LOG_CHANNEL_ID`
- Geschlossene Tickets können über `ARCHIVED_TICKET_CATEGORY_ID` in eine Archiv-Kategorie verschoben werden; der Ticket-Ersteller verliert dabei den Zugriff.
- Geschlossene Tickets werden als PDF-Transkript im über `/set_ticket_transcript_channel` konfigurierten Staff-Kanal gespeichert und zusätzlich an den Ersteller per DM gesendet.
- Über `/set_suggestion_channel` und `/suggestions_setup` kann ein Suggestions-Dashboard eingerichtet werden; Vorschläge werden im Zielkanal gespeichert und können dort von Staff angenommen oder abgelehnt werden.
- Über `/set_referral_earning_channel` kann ein Kanal für Referral-Hinweise gesetzt werden. Die Nachricht nennt nur Empfehlenden und empfohlenen Booster, ohne Verdienstbetrag.
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
