# Netatmo Enhanced 0.5.0 sperimentale

Integrazione non ufficiale per BTicino Smarther BNS su Home Assistant. La modalità sperimentale **Aggiornamento diretto (Cloud Push)** aggiorna le entità con i dati autenticati del webhook Netatmo. L’aggiornamento mantiene il dominio `netatmo_enhanced`, la voce d’integrazione, gli identificativi delle entità e del dispositivo, lo storico, le credenziali OAuth e la registrazione webhook esistente. Netatmo Enhanced può convivere con Netatmo e Netatmo Sync Fix.

## Installazione tramite Marketplace (Home Assistant 2026.11+)

A installazione esistente, abilita **Pre-release** nella scheda Marketplace del repository quando la 0.5.0 è disponibile. Installa l’aggiornamento e riavvia Home Assistant mantenendo la stessa voce d’integrazione. Per la prima installazione aggiungi `https://github.com/Luchino15/Netatmo-Enhanced` da **Impostazioni → Marketplace → Aggiungi da link**. [Installazione, collaudo e rollback alla 0.4.0](docs/collaudo-0.5.0.md).

Per installare manualmente, fai un backup, sostituisci `/config/custom_components/netatmo_enhanced/` con la cartella omonima dall’archivio sorgente della versione scelta e riavvia Home Assistant mantenendo la voce d’integrazione. Nelle installazioni nuove i webhook partono disattivati. Negli aggiornamenti si conserva la modalità già impostata.

## Boost e regolazione manuale

Il preset **Boost** avvia un ciclo di 90 minuti nella stagione attiva. Per scegliere la durata, passare alla successiva o interrompere Boost, usa le azioni della tabella. Seleziona come destinazione l’entità climate di Netatmo Enhanced. Le azioni sono disponibili in **Strumenti per sviluppatori → Azioni**, negli script e nelle automazioni.

| Azione | Parametri e comportamento |
|---|---|
| `netatmo_enhanced.set_boost` | `duration` in minuti, a scelta tra `30`, `60` e `90`. |
| `netatmo_enhanced.cycle_boost` | Avvia 30 minuti se inattivo, poi passa a 60, a 90 e infine interrompe Boost. Il ciclo usa la durata confermata se nota. |
| `netatmo_enhanced.stop_boost` | Termina Boost restituendo il controllo alla modalità applicabile alla stanza. |
| `netatmo_enhanced.set_manual_temperature` | `temperature` in °C a passi di 0,5 e `duration` in minuti interi da 1 a 720. Intervallo 7–30 °C in riscaldamento e 16–30 °C in raffrescamento. |

La scadenza effettiva dell’override viene dal termostato ed è visibile nel sensore **Scadenza regolazione temporanea** quando comunicata. La scheda Entità può mostrare l’ora e il tempo residuo senza nuove letture API. Se all’avvio è presente un Boost di durata iniziale sconosciuta, usa **Imposta Boost** con una durata esplicita oppure **Interrompi Boost**. Il valore residuo da solo non identifica la durata di partenza. [Esempio di scheda Entità](docs/collaudo-0.5.0.md#prove-boost).

Durante Boost il termostato può comunicare un setpoint nullo. In quel caso la temperatura richiesta resta vuota e l’azione climate segue lo stato del modulo. Lo Smarther può avviare Boost dall’app o dal display, con durate di 30, 60 e 90 minuti. [Manuale BTicino Smarther2](https://www.bticino.it/sites/default/files/2025-04/catalogo-smarther2-manuale-utente.pdf).

La diagnostica della 0.4.0 ha rilevato sul campo il Boost in riscaldamento avviato e interrotto da app e display. Le nuove scritture in entrambe le stagioni usano l’API pubblica Home + Control e vanno collaudate su BNS. [API Netatmo `setstate`](https://dev.netatmo.com/apidocumentation/control#setstate), [prove Boost sul termostato](docs/collaudo-0.5.0.md#prove-boost).

## Raffrescamento e programmazione

In raffrescamento Home Assistant espone **Spento** e **Auto**. Con la casa in Programmazione, **Auto** ripristina la modalità automatica della stanza. Il selettore mostra le programmazioni della stagione attiva. Il cambio stagione si esegue in Home + Control. La scrittura di **Assente** estivo resta fuori dalle funzioni supportate. In riscaldamento l’integrazione espone **Antigelo**. [Cambio stagione su Smarther](https://support.legrand.com/hc/en-us/articles/27800001882642-How-can-I-switch-from-heating-to-cooling-mode-on-my-Smarther-with-Netatmo-smart-thermostat), [API Netatmo `setstate`](https://dev.netatmo.com/apidocumentation/control#setstate).

## Modalità webhook

Apri **Impostazioni → Dispositivi e servizi → Netatmo Enhanced → Configura** e scegli la modalità.

| Modalità | Aggiornamento delle entità |
|---|---|
| Disattivato | Polling periodico e letture API richieste da Home Assistant o dai comandi. |
| Solo ricezione | Stesse letture API, con diagnostica delle notifiche webhook autenticate. |
| Ricezione e aggiornamento API | Le notifiche pertinenti avviano un ciclo API accorpato, con almeno 60 secondi dall’inizio dell’ultima lettura del coordinatore. |
| Aggiornamento diretto (Cloud Push) | I campi supportati aggiornano subito le entità. I dati incompleti o ambigui possono avviare la riconciliazione API con lo stesso intervallo minimo. Con il polling abilitato in Home Assistant, la verifica di riserva continua ogni 10 minuti. |

Il passaggio tra le modalità si applica alla voce già caricata. Per tornare alla strategia con lettura API dopo i webhook seleziona **Ricezione e aggiornamento API**. Per ripristinare la versione precedente, installa la 0.4.0 dal Marketplace oppure dal relativo archivio sorgente, conservando la voce esistente. [Rollback e verifica](docs/collaudo-0.5.0.md#rollback-alla-040).

La classificazione Marketplace `cloud_polling` resta valida per la configurazione predefinita e per le modalità precedenti. La sola modalità diretta usa Cloud Push come sorgente primaria con polling di riserva.

## Ricezione e aggiornamento

- Con proxy o tunnel, inoltra `POST /api/webhook/<id>` all’URL esterno HTTPS di Home Assistant preservando corpo e intestazione `X-Netatmo-secret`. Tratta il percorso completo come riservato.
- La firma HMAC-SHA256 viene verificata sul corpo originale con il client secret dell’applicazione personale e un confronto a tempo costante. L’adattatore accetta corpi fino a 256 KiB e concede fino a 3 secondi per leggerli. Una firma mancante o errata viene rifiutata con HTTP 403.
- L’identificativo casa può arrivare come `home_id` o `home.id` nella radice o in `extra_params`. La modalità diretta aggiorna i campi presenti per case, stanze e moduli già noti. Gli aggiornamenti tengono conto del singolo campo o gruppo di campi e di `sequence_id` quando disponibile. La modalità globale, la modalità stanza, la stagione e la programmazione rimangono distinte. Lo stato operativo deriva dalle richieste del modulo e il setpoint resta quello comunicato dal termostato, anche durante il preriscaldamento.
- Una programmazione sconosciuta, una topologia cambiata o dati ambigui fanno accodare una verifica API. Le risposte API che si sovrappongono a un push conservano i campi più recenti e la protezione si esaurisce dopo una finestra limitata. Verifiche ravvicinate vengono accorpate. Dopo l’aggiunta di un nuovo Smarther, ricarica la voce per creare le sue entità.
- I comandi Home Assistant mantengono la verifica iniziale e possono essere confermati da un push pertinente. Se la conferma manca, è prevista una lettura API. Il ritorno alla programmazione mantiene l’uso di `setstate` con `therm_setpoint_mode=home` per chiudere la modifica manuale.

## Registrazione webhook v1

| Endpoint | Comportamento |
|---|---|
| `GET /webhooks/v1/` | Elenca le registrazioni per l’autorizzazione corrente. Il formato `body: [{ "url": "…" }]` è confermato dal client pyatmo. |
| `POST /webhooks/v1/` | Aggiunge un singolo URL da un oggetto JSON con chiave `url`. La consegna dipende dagli scope concessi e dall’abilitazione dell’app. HTTP 409 indica il limite di registrazione. |

L’host API è `https://api.netatmo.com`. Il codice usa il percorso senza slash finale adottato dal client pyatmo e disabilita i redirect. Fa un GET all’avvio della modalità webhook e un solo POST se il GET restituisce un elenco valido e l’URL manca. Se il POST risponde 2xx senza confermare l’URL, attende due secondi e fa al massimo un GET di verifica, soggetto al cooldown esistente. Un 409 lascia intatte le registrazioni. La disattivazione rimuove la rotta locale. L’URL remoto può rimanere registrato e occupare uno slot, anche dopo un rollback o un cambio di dominio.

## Diagnostica e limiti

- Scarica la diagnostica dal menu della voce **Netatmo Enhanced** in **Impostazioni → Dispositivi e servizi**. Contiene fino a 100 eventi autenticati e 40 eventi di controllo, con esiti, identificativi pseudonimizzati, latenze e contatori HTTP e del coordinatore. Il tempo dalla ricezione alla pubblicazione misura il tratto interno a Home Assistant. [Diagnostica dei controlli e misurazione](docs/collaudo-0.5.0.md#preparazione-e-misurazione).
- `invalid_external_https_url` indica che Home Assistant non ha fornito un URL esterno HTTPS utilizzabile. `get_http_401` o `post_http_401` indicano un problema di autorizzazione e `403` può dipendere dall’abilitazione dell’app. Un `post_http_409` lascia intatte le registrazioni esistenti. Dopo aver risolto la causa, ricarica la voce per ripetere il GET. Per un 429 attendi la scadenza del cooldown.
- Ogni ciclo API legge l’inventario e lo stato di ogni casa con Smarther BNS. Il cooldown HTTP 429 parte da almeno 15 minuti, può essere prolungato da `Retry-After` ed è locale alla voce. Timeout e indisponibilità temporanee ricadono sui meccanismi di recupero del coordinatore.
- Nel collaudo della 0.4.0 su Raspberry Pi 4, 84 aggiornamenti diretti hanno avuto latenza interna mediana di 3,814 ms e massima di 6,228 ms. I test offline della 0.5.0 usano fixture sintetiche e trasporto simulato. Il collaudo delle nuove scritture e della scadenza effettiva richiede il termostato. [Procedura e misurazione](docs/collaudo-0.5.0.md).

Test da eseguire dalla radice dell’archivio con `python -m unittest discover -s tests -v`.

Riferimenti per registrazione, autenticazione e ricezione. [Netatmo Connect Webhooks](https://dev.netatmo.com/apidocumentation/webhooks), [costanti pyatmo](https://github.com/jabesq-org/pyatmo/blob/development/src/pyatmo/const.py), [registrazioni pyatmo](https://github.com/jabesq-org/pyatmo/blob/development/src/pyatmo/auth.py), [webhook HA Core 2026.11.3](https://github.com/home-assistant/core/blob/2026.11.3/homeassistant/components/webhook/__init__.py), [OAuth HA Core 2026.11.3](https://github.com/home-assistant/core/blob/2026.11.3/homeassistant/helpers/config_entry_oauth2_flow.py).
