# Driftunderhåll – prototyp v0.3

En första körbar prototyp för ett litet drift-/underhållssystem.

## Det som finns i v0.1

- PostgreSQL från första start
- två demoanvändare:
  - `admin@demo.local` – admin
  - `pelle@demo.local` – tekniker
- användarna skapas **utan lösenord**
- aktivering via mejllänk → användaren väljer lösenord + telefonnummer
- återställning av lösenord via mejllänk
- extern felanmälan utan login
- unik, hashad spårningslänk för anmälaren
- bilduppladdning (JPG/PNG/WebP, max 8 MB som standard)
- PIN-/kodskyddad central dashboard utan personlig inloggning
- dashboard får tilldela felanmälningar men kan inte avsluta eller radera
- mejl när ett ärende tilldelas
- inloggad arbetssida för admin/tekniker
- FU och egenkontroller med delmoment och kommentarer
- delmoment kan skapa en ny felanmälan och tilldelas direkt
- enkel audit-logg för viktiga händelser
- Mailpit lokalt för att prova alla mejl

## Start

```bash
cp .env.example .env
docker compose up --build -d
```

Följ loggen:

```bash
docker compose logs -f app
```

## Adresser

- Extern felanmälan: `http://SERVER:18090/report`
- Central dashboard: `http://SERVER:18090/dashboard`
- Personlig login: `http://SERVER:18090/login`
- Mailpit: `http://SERVER:8025`

Dashboard-koden är som standard `246810` och ändras i `.env`.

## Aktivera demoanvändarna

Vid första starten skickas aktiveringsmejl automatiskt till Mailpit för:

- `admin@demo.local`
- `pelle@demo.local`

Öppna Mailpit på port 8025 och klicka på aktiveringslänken i respektive mejl. Om ett mejl skulle saknas finns **Skicka ny aktiveringslänk** på login-sidan.

Lösenordskrav i prototypen:
- minst 8 tecken
- minst en bokstav
- minst en siffra

## Viktigt inför produktion

Det här är en prototyp för lokal utveckling. Inför GleSYS bör vi minst:

- sätta `SESSION_COOKIE_SECURE=true`
- lägga Caddy/reverse proxy framför appen
- köra HTTPS
- byta alla secrets och dashboard-kod
- koppla riktig SMTP
- lägga backup av PostgreSQL och uppladdade filer på separat plats
- införa riktiga databasmigrationer
- granska loggning, retention och behörigheter
- byta från delad dashboard-kod till registrerade dashboard-enheter om vi vill höja nivån

## v0.1.1

- Fixar namnkonflikt mellan SQLAlchemy-relationen `property` och Pythons `@property`.
- Kör en Gunicorn-worker i prototypen för att undvika race vid första seedningen av demodata.


## v0.2

- Alla interna vyer använder nu i princip hela 16:9-arbetsytan.
- Extern felanmälan är telefon-först och hålls smal även på desktop.
- Felanmälare kan välja känd plats **eller skriva platsen själv**.
- `Slutför FU/EG` sparar samtliga kryss och kommentarer i samma klick.
- `Spara framsteg` finns kvar för delvis utfört arbete.
- Arbetsorder kan öppnas i en separat informationsvy med delmomentsinstruktioner,
  referenslänk/manual och hjälpbild där sådant finns.
- Dashboard pollar servern var 5:e sekund och laddar om när data ändras.
- Dashboard gör dessutom en tidsuppdatering varje minut.
- Felanmälningar yngre än 30 minuter får märkningen **Nyligen inkommit**.
- Felanmälningar på dashboard är klickbara och öppnas i en stor popup med
  full text, eventuell bild och tilldelning.
- Nya fält läggs till med en minimal PostgreSQL-migrering vid startup så
  befintlig utvecklingsdatabas kan återanvändas.

### Uppgradera från v0.1.1 utan att radera databasen

Kopiera v0.2-filerna över din befintliga projektkatalog och kör:

```bash
docker compose down
docker compose up --build -d
docker compose logs -f app
```

Kör **inte** `docker compose down -v` om du vill behålla databas och uppladdningar.


## v0.3 – chefsversion

- Prioritet för FU/EG är kraftigt förstärkt på central dashboard.
- Felanmälningar visar ansvarig direkt på dashboardkortet.
- FU och EG är klickbara på dashboard och öppnas i samma typ av popup som felanmälningar.
- FU/EG-popup visar delmoment, instruktioner, framsteg och aktuella mätvärden.
- FU/EG kan tilldelas en person direkt från dashboard; personen får mejl.
- Dashboard visar även vem FU/EG redan är tilldelad till.
- Fullvyn **Ta fram arbetsordern** är nu en riktig arbetsvy:
  - bocka delmoment
  - skriv kommentarer
  - registrera avvikelse
  - ange ansvarig för avvikelsen
  - spara framsteg
  - slutför FU/EG
- Datamodellen stödjer numeriska kontrollvärden per mallpunkt:
  - minvärde
  - maxvärde
  - enhet
  - registrerat värde
- Ett värde utanför tillåtet intervall får registreras eftersom kontrollen faktiskt är utförd,
  men systemet skapar då automatiskt en felanmälan.
- `Filterstatus` i demonstrations-FU är satt till intervallet 0–10 för att funktionen ska gå att visa direkt.
- Befintlig PostgreSQL-volym kan återanvändas. v0.3 lägger till de nya kolumnerna automatiskt.

### Uppgradering från v0.2

Kopiera update-paketet över befintlig projektmapp och kör:

```bash
docker compose down
unzip -o underhall_v0_3_update.zip
docker compose up --build -d
docker compose logs --tail=100 app
```

Kör inte `docker compose down -v` om databasen och uppladdningarna ska behållas.
