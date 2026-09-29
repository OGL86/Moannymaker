# Moannymaker

Regelbasert trading-bot for memecoins på Solana, med web-dashboard og kopitrading.

Boten kjøper etablerte memecoins billig i faste omganger, tar små sjanser på nyere coins som har
bestått en sikkerhetssjekk, og kan følge utvalgte wallets på pump.fun i sanntid. Et eget risikolag
i ren kode har alltid siste ord, og alt kjører i **øvingsmodus** til du bevisst slår på ekte handel.

> **Viktig:** Dette er et verktøy, ikke finansiell rådgivning. Memecoins er ekstremt volatile, og de
> fleste som handler dem taper penger. Bruk bare penger du tåler å tape helt. Koden er ikke testet mot
> ekte markedsdata ennå – se [Status](#status).

---

## Innhold

- [Funksjoner](#funksjoner)
- [Hurtigstart](#hurtigstart)
- [Dashboard](#dashboard)
- [Slik fungerer boten](#slik-fungerer-boten)
- [Kopitrading](#kopitrading)
- [Sikkerhet og risikostyring](#sikkerhet-og-risikostyring)
- [Ekte handel (live)](#ekte-handel-live)
- [Kommandoer](#kommandoer)
- [Konfigurasjon](#konfigurasjon)
- [Prosjektstruktur](#prosjektstruktur)
- [Tester](#tester)
- [Skatt (Norge)](#skatt-norge)
- [Status](#status)

---

## Funksjoner

| | |
|---|---|
| **Fire potter** | Grunnmur (etablerte coins), rotasjon, småsatsinger og kopitrading – hver med sin andel av kapitalen |
| **Kapital vokser bare fra realisert gevinst** | Aldri fra urealisert gevinst, og aldri fra påfyll utenfra |
| **Sikkerhetsfilter** | Alder (14+ dager), likviditet, volum, tidligere fall, holderkonsentrasjon, mint/freeze authority og farlige Token-2022-utvidelser |
| **Risikolag** | Maks per kjøp, maks tap per dag, maks handler per måned, nødbrems |
| **Kopitrading** | Følg wallets på pump.fun i sanntid via PumpPortal, med ekte statistikk på hvem som faktisk tjener penger |
| **Dashboard** | Nybegynnervennlig web-grensesnitt med status, forklaringer i klartekst og innstillinger |
| **Backtest** | Samme strategi- og risikokode som live, på CSV-filer eller GeckoTerminal-data |
| **Varsler** | Telegram ved hvert kjøp, salg, blokkering og feil |
| **Skatterapport** | FIFO-beregning med USD/NOK-kurs fra Norges Bank |
| **Docker** | Én kommando starter alt |

---

## Hurtigstart

Krever Python 3.11+.

```bash
git clone https://github.com/OGL86/Moannymaker.git
cd Moannymaker

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # fyll inn RPC-URL, og eventuelt Telegram
python -m pytest -q                # sjekk at alt virker (kjører uten nettverk)
```

Start så tre prosesser, gjerne i hvert sitt terminalvindu:

```bash
python -m memebot dashboard        # åpne http://127.0.0.1:8080
python -m memebot listen           # finner nye kandidater fra pump.fun
python -m memebot run              # selve boten, i øvingsmodus
```

Åpne dashboardet og følg «Kom i gang»-sjekklisten. Der legger du også til coins uten å redigere filer.

### Med Docker

```bash
cp .env.example .env
touch config.yaml                  # må finnes før første oppstart
docker compose up -d --build       # listener + bot + dashboard
docker compose --profile copy up -d   # i tillegg: kopitrading
```

---

## Dashboard

`python -m memebot dashboard` → **http://127.0.0.1:8080**

Laget for å være lett å forstå uten forkunnskaper om trading:

- **Status i klartekst** øverst, for eksempel «Boten går i øvingsmodus – ingen ekte penger brukes»
- **Stopp all handel**: stor nødbrems som virker fra neste runde
- **Kom i gang**-sjekkliste som forsvinner når alt er satt opp
- **Verdi over tid** som graf, med startbeløpet som referanselinje
- **Fordeling i pottene**: hvor mye som er kjøpt inn, og hvor mye som er ledig
- **Ett kort per coin** med gevinst/tap og vei mot neste gevinstsikring
- **Hva har skjedd**: hvert kjøp og salg forklart med vanlige ord
- **Wallets jeg følger**: resultat, treffprosent og holdetid for walletene du kopierer, med en vurdering
- **Hvorfor boten sa nei**: hvilke coins som ble avvist, og hvorfor
- **Innstillinger**: startbeløp, grenser, coins, pottfordeling og kopitrading
- **Ordliste** som forklarer alle begrepene

Om boten handler med ekte penger kan bevisst **ikke** endres fra dashboardet.

Dashboardet lytter bare på din egen maskin. Skal du nå det fra mobilen, bruk en VPN som Tailscale og
sett `DASHBOARD_TOKEN` i `.env`. Da må adressen ha `?token=...`.

---

## Slik fungerer boten

```
 DexScreener ──┐                          ┌──> Papirhandel (simulert)
 GeckoTerminal ┼─> Data ─> Filter ─> Strategi ─> RISIKOLAG ─┤
 Solana RPC ───┤                          │                 └──> Live: PumpPortal / Jupiter
 PumpPortal ───┘                          │                            (signeres lokalt)
                                          v
                                  SQLite-logg ──> Dashboard + Telegram
```

Hver runde (standard hvert 15. minutt):

1. Henter priser og daglige candles for alle coins boten følger eller eier
2. Finner kandidater: tokens som migrerte fra pump.fun for 14+ dager siden, og trending tokens
3. Kjører et billig forfilter, og deretter on-chain-sjekker på de som gjenstår
4. Strategien foreslår ordre: salg først, deretter grunnmur, rotasjon og nye småsatsinger
5. **Risikolaget** godkjenner, skalerer ned eller blokkerer hver ordre
6. Utfører, logger og varsler

### Strategiregler

Alle tall kan endres i `config.yaml`.

**Grunnmur (60 %)** – etablerte community-coins
- Kjøpes i 3 omganger, med minst 5 dager mellom hver
- Kjøper bare når prisen er i nederste 25 % av 30-dagers prisområde, og bare på røde dager
- Selger 25 % ved +100 %, deretter ved +200 % osv.
- Rotasjon: når én coin er nær toppen av sitt område og en annen nær bunnen, flyttes 25 % (maks én gang per uke)

**Rotasjon (25 %)** – f.eks. launchpad-tokens
- Kjøper halv størrelse nær bunnen av prisområdet og selger nær toppen

**Småsatsinger (15 %)** – nyere coins
- Maks 5 åpne samtidig
- Må bestå sikkerhetsfilteret, ha stigende volum og grønn dag, og ligge minst 50 % under toppen
- Selger 25 % ved +100 % og 25 % ved +200 %, og har deretter break-even-stopp
- Ved −70 % regnes den som tapt, og det kjøpes aldri mer

**Kopitrading (0 % som standard)** – se neste seksjon

---

## Kopitrading

Følger utvalgte wallets via PumpPortal og handler i samme øyeblikk som de gjør.

- **Speil**: kjøper med en gang én leder kjøper
- **Konsensus**: kjøper først når flere ledere har kjøpt samme coin innen et tidsvindu
- Selger samme andel som lederen selger
- Egne exits i tillegg: gevinstsikring, stop-loss og maks holdetid
- Rask sikkerhetssjekk før hvert kjøp: mint/freeze authority og farlige Token-2022-utvidelser
- Egen pott og egen dagsgrense. Salg går alltid gjennom.
- **Alle handlene til walletene loggføres**, så dashboardet viser hvem som faktisk tjener penger, og advarer
  mot wallets der nesten all gevinst kommer fra én coin

**Krav:** `PUMPPORTAL_API_KEY` i `.env` (lages på [pumpportal.fun](https://pumpportal.fun)), med minst 0,02 SOL i
walleten som er koblet til nøkkelen. Handelsstrømmen koster 0,01 SOL per 10 000 hendelser.

**Anbefalt:** La kopiering være **av** i 1–2 uker, slik at boten samler statistikk før du slår den på.
I øvingsmodus får du bevisst en dårligere pris enn lederen, fordi du i virkeligheten alltid kommer litt etter.

---

## Sikkerhet og risikostyring

**Risikolaget** er ren kode som strategien ikke kan overstyre:

- Maks beløp per kjøp
- Maks tap per dag – når grensen nås, blokkeres nye kjøp
- Maks handler per måned (kopitrading har egen dagsgrense)
- **Nødbrems**: knappen i dashboardet, eller fila `data/KILL`
- Salg som beskytter kapital slipper alltid gjennom

**Sikkerhetsfilteret** avviser coins som:

- er yngre enn 14 dager, eller har for lite likviditet eller volum
- ikke har overlevd et fall på minst 70 % ennå
- har for konsentrert eierskap (topp 10 holdere, likviditetspoolen er ikke med i tellingen)
- fortsatt har *mint authority* (utsteder kan lage nye tokens) eller *freeze authority* (utsteder kan fryse walleten din)
- har farlige Token-2022-utvidelser: *permanent delegate*, *transfer hook*, overføringsskatt, *non-transferable* eller frosne kontoer som standard

**Nøkler og hemmeligheter**

- Privatnøkkelen ligger bare lokalt og sendes aldri noe sted. Transaksjoner signeres på din maskin.
- `.env`, `config.yaml`, nøkkelfiler (`*.json`) og databasen er i `.gitignore` og `.dockerignore`
- Dashboardet har CSRF-vern og lytter bare på `127.0.0.1` som standard

---

## Ekte handel (live)

Live krever **to** bevisste valg: `mode: live` i `config.yaml` **og** flagget `--live`.

1. Kjør i øvingsmodus i flere uker, og se at resultatene holder
2. `python -m memebot wallet-new` – lag en **egen** wallet kun for boten
3. Sett inn bare det beløpet boten skal ha, og sett `start_capital_usd` til det samme
4. Sett `SOLANA_KEYPAIR_PATH` og en egen RPC-URL (f.eks. Helius eller QuickNode) i `.env`
5. Sett `mode: live` i `config.yaml`
6. `python -m memebot --live run` (og eventuelt `python -m memebot --live copy`)
7. Følg med i dashboardet og på Telegram

Live-handel går via PumpPortal Local API (0,5 % gebyr) eller Jupiter. Før hvert vanlige kjøp sjekkes
price impact, og faktisk fyll beregnes fra saldoendringen on-chain.

---

## Kommandoer

| Kommando | Hva den gjør |
|---|---|
| `python -m memebot dashboard` | Web-dashboard på http://127.0.0.1:8080 |
| `python -m memebot run` | Boten i løkke (leser config på nytt hver runde) |
| `python -m memebot once -v` | Én runde, og viser hvorfor kandidater ble avvist |
| `python -m memebot listen` | Lytter på pump.fun-migrasjoner (kandidatkilde) |
| `python -m memebot copy` | Kopitrading i sanntid |
| `python -m memebot check <MINT>` | Kjør sikkerhetsfilteret på én coin |
| `python -m memebot status` | Potter, posisjoner og siste hendelser |
| `python -m memebot backtest core:SYM=fil.csv ...` | Backtest med samme kode som live |
| `python -m memebot tax --year 2026` | FIFO-rapport med USD/NOK |
| `python -m memebot wallet-new` | Lag egen bot-wallet (nøkkelfil med rettighet 600) |
| `python -m memebot wallet` | SOL-saldo i bot-walleten |

Legg `--live` **før** kommandoen for live, f.eks. `python -m memebot --live run`.

### Backtest

```bash
# CSV-format: ts,open,high,low,close,volume (daglige candles)
python -m memebot backtest \
  core:SPX=data/spx.csv core:FART=data/fart.csv \
  rotation:PUMP=<pool-adresse> shots:LMAO=<pool-adresse> \
  --out trades.csv
```

Resultatet viser avkastning, maks drawdown, antall handler, gebyrer og en sammenligning med kjøp-og-hold.

---

## Konfigurasjon

| Fil | Innhold |
|---|---|
| `config.example.yaml` | Alle innstillinger med forklaring og standardverdier |
| `config.yaml` | Dine endringer (overstyrer standardverdiene). Lages automatisk fra dashboardet. |
| `.env` | Hemmeligheter: RPC-URL, nøkkelsti, Telegram, PumpPortal, dashboard-token |

Du trenger bare å skrive det du vil endre i `config.yaml`. Resten hentes fra `config.example.yaml`.

---

## Prosjektstruktur

```
memebot/
├── __main__.py      Kommandolinje
├── config.py        Laster config.yaml og .env
├── data.py          DexScreener, GeckoTerminal og Solana RPC
├── filters.py       Sikkerhetsfilter og Token-2022-sjekk
├── strategy.py      Strategiregler (rene funksjoner)
├── risk.py          Risikolaget
├── portfolio.py     Posisjoner og potter, avledet fra handelsloggen
├── engine.py        Hovedløkka
├── execution.py     Papir- og live-utførelse (signering lokalt)
├── pumpportal.py    Migrasjonsstrøm og Local Transaction API
├── copytrade.py     Kopitrading og wallet-statistikk
├── backtest.py      Backtest
├── dashboard.py     Web-server for dashboardet
├── web/index.html   Dashboardet
├── storage.py       SQLite
├── notify.py        Telegram
└── tax.py           FIFO og USD/NOK
tests/               28 tester, kjører uten nettverk
```

---

## Tester

```bash
python -m pytest -q
```

Testene dekker strategi, risikolag, filtre, portefølje, FIFO, backtest, hele hovedløkka med falske
datakilder, dashboard-API-et, kopitrading og lokal signering av Solana-transaksjoner.

---

## Skatt (Norge)

`python -m memebot tax --year 2026` lager en CSV med alle realiserte gevinster og tap etter FIFO, med
USD/NOK-kurs fra Norges Bank.

Hver swap er en realisasjon. Rapporten dekker token-siden av hver handel, men **ikke** SOL-siden
(SOL→token er også en realisasjon av SOL). Bruk et kryptoskatteverktøy med wallet-import til selve
skattemeldingen, og denne rapporten som kontroll.

---

## Status

Moannymaker er et tidlig prosjekt:

- **Ikke testet mot live-API-ene ennå.** Utviklingsmiljøet hadde ikke tilgang til DexScreener,
  GeckoTerminal eller PumpPortal. Logikken er testet med falske datakilder, og signeringen med ekte
  Solana-transaksjoner offline. Kjør `once -v` og `check` i øvingsmodus først, og se at dataene stemmer.
- **Ingen bevist edge.** Strategien er ikke validert på ekte historikk. Mål resultatene i øvingsmodus
  før du bruker ekte penger.
- Endepunkter hos tredjeparter kan endre seg. Base-URL-ene ligger i config.
- GeckoTerminals gratis-API er tregt (~30 kall/min), så en runde kan ta 1–2 minutter.
- Dagens candle er uferdig, så volumsignalet undervurderer volumet tidlig på dagen (UTC).
- «Holdere som vokser» er ikke implementert.
- Backtest av småsatsinger gir survivorship bias: du tester bare coins du vet overlevde.

### Planlagt

- Resultatmåling per pott (forventet verdi per handel, profit factor)
- Måling av etterslep i kopitrading (lederens pris mot din pris)
- Salgssimulering før kjøp (honeypot-sjekk)
- Deteksjon av dev-wallets og bundlede kjøp
- Markedsfilter (ikke kjøp når meme-markedet faller)

---

*Ikke finansiell rådgivning. Du er selv ansvarlig for hvordan du bruker programvaren og for eventuelle tap.*
