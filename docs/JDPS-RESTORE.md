# Underhåll – JDPS driftsättning och återställning

## Miljö
- Repo: `joelwedberg/underhall`, utvecklingsbranch `dev/jdps-deploy` och skyddad originalbranch `backup/pre-jdps-20261024`.
- Serverkatalog: `/home/joel/underhall`; Compose-projekt `underhall`.
- Webbtjänst: port 18090, Mailpit (endast utvecklingsmejl): port 8025.
- PostgreSQL 16 med volym `underhall_underhall_pg`; uppladdningar i `underhall_underhall_uploads`. Gamla prototypvolymer `underhall_prototype_v0_1_*` lämnas orörda.
- Hemligheter i lokal `.env`, ska aldrig versionshanteras. Första installationen använder lokalt slumpgenererade värden, inte prototypens standardlösenord.
- Status: **utvecklingsinstallation**, inte säkerhetsgranskad produktionsdrift eller redo för känsliga ärenden.

## Deploy
Kör `sudo bash scripts/deploy.sh` i projektkatalogen. Skriptet kontrollerar ren Git-branch, tar snapshot av `.env`, befintlig PostgreSQL via `pg_dump`, och uppladdningsvolym via `tar` om den finns. Sedan startar det bara Compose-projektet `underhall` och inväntar appens Docker-healthcheck.

**Avbrott:** endast de nya `underhall`-containrarna kan startas om. Samma dator kör andra appar; de påverkas inte.

## Återställning (manual, ej testad)
1. Stoppa `underhall` (ej gamla prototypen), identifiera önskad snapshot i `.jdps/backups/` och motsvarande Git-commit och `.env`. Kontrollera först att ingen ny verksamhetsdata måste sparas. Kopiera alltid befintliga volymer före återläsning.
2. Återställ `.env` från skyddad backup och checka ut relevant Git-commit i en **separat** arbetskopia. Bygg applikationsimagen enligt denna commit.
3. För PostgreSQL: använd `pg_restore` mot ny eller kontrollerat tömd databas (format `pg_dump -Fc`). För uppladdningar: återför `uploads.tar.gz` till rätt volym efter att appen stoppats. Starta tjänsterna och kontrollera /report, kontoaktivering och uppladdningar.
4. Kontrollera att databasen, uppladdningarna och appversionen hör ihop. Testa återställningen på LAB innan verklig incident.

**Återställning är inte verifierad.** Lokala kopior är inte oberoende offsite-backup. Databasmigrationer från v0.1 och produktion med extern SMTP/HTTPS hanteras separat.
