# Netatmo Enhanced 0.3.0 sperimentale

Aggiornamento della 0.2.1 originale per BTicino Smarther BNS. Mantiene il dominio `netatmo_enhanced`, gli ID univoci, le credenziali e gli scope OAuth. I file delle entità, il modello e i comandi sono identici alla 0.2.1, incluso il ritorno alla programmazione da Manuale, Assente e Antigelo.

## Installazione tramite Marketplace (Home Assistant 2026.11+)

Aggiungi `https://github.com/Luchino15/Netatmo-Enhanced` in Impostazioni → Marketplace → Aggiungi da link, quindi installa la release desiderata. Per passare dall’installazione manuale, conserva prima un backup della cartella e mantieni la voce di integrazione già configurata. Non eliminare la voce per installare un aggiornamento. Riavvia Home Assistant dopo la sostituzione dei file.

Integrazione non ufficiale; la 0.3.0 è sperimentale, i webhook partono disattivati e la consegna reale non è ancora stata collaudata.

## Installazione manuale e collaudo

1. Conserva lo ZIP 0.2.1 e fai un backup di Home Assistant. Estrai questo ZIP e sostituisci soltanto `/config/custom_components/netatmo_enhanced/` con la cartella omonima nell’archivio. Riavvia Home Assistant. Mantieni la voce d’integrazione esistente.
2. Con webhook **Disattivato**, verifica che le entità precedenti siano disponibili e che il loro ID sia quello atteso. Il polling di Home Assistant continua ogni 10 minuti con entità attive e polling abilitato.
3. Per il collaudo serve l’URL esterno HTTPS di Home Assistant. Con un proxy o un tunnel, la rotta `POST /api/webhook/<id>` deve arrivare a Home Assistant senza una pagina di login intermedia, preservando il corpo e l’intestazione `X-Netatmo-secret`. Usa il dominio pubblico già predisposto. Tratta il percorso completo come riservato.
4. In **Impostazioni → Dispositivi e servizi → Netatmo Enhanced → Configura**, seleziona **Solo ricezione**. La voce si ricarica. Dopo qualche secondo scarica la diagnostica. `registered` o `already_registered` indica che l’URL risulta registrato.
5. Dall’app del termostato fai una modifica reversibile a un setpoint o alla programmazione. Riscarica la diagnostica. Cerca un incremento di `device_event` o `home_event_changed`. Un incremento di `webhook_activation` indica solo che è arrivata una notifica di attivazione. In questa modalità `refresh_success` deve restare a zero.
6. Se arrivano eventi pertinenti, seleziona **Ricezione e aggiornamento API**. Dopo il ricaricamento e il primo aggiornamento periodico di avvio, fai una sola modifica dall’app. Controlla che aumenti `refresh_success` e che lo stato in HA coincida con quello dell’app. Il refresh attende almeno 60 secondi dall’inizio dell’ultima lettura del coordinatore se necessario, oltre a eventuali cooldown e comandi in corso. I contatori ripartono da zero al ricaricamento.
7. Ripeti una volta l’operazione inversa. Verifica poi il ritorno alla programmazione da Manuale, Assente e Antigelo, un comando alla volta. Per tornare al solo polling, seleziona **Disattivato**. Per ripristinare la release precedente, sostituisci la cartella con la 0.2.1 e riavvia, tenendo la stessa voce d’integrazione.

## Contratto ricavato dai sei PDF

| Stampa | Endpoint | Comportamento |
|---|---|---|
| 1 | `POST /api/addwebhook` | Registrazione storica deprecata, con URL nella query. |
| 2 | `POST /api/dropwebhook` | Rimozione storica deprecata. |
| 3 | `GET /webhooks/v1/` | Elenco delle registrazioni per l’autorizzazione corrente. La stampa mostra la scheda di prova, senza esempio della risposta. Il formato `body: [{ "url": "…" }]` è confermato dal client pyatmo. |
| 4 | `POST /webhooks/v1/` | Aggiunge un singolo URL da un oggetto JSON con chiave `url`. Il Bearer token esistente basta per la richiesta, mentre la consegna dipende dagli scope concessi e dall’abilitazione dell’app. HTTP 409 indica il limite di registrazione. |
| 5 | `PUT /webhooks/v1/` | Sostituisce l’intero elenco con una lista di oggetti contenenti `url`. |
| 6 | `DELETE /webhooks/v1/` | Elimina tutte le registrazioni nel contesto della richiesta. |

L’host API è `https://api.netatmo.com`. Il codice usa il percorso senza slash finale adottato dal client pyatmo e disabilita i redirect. Fa un GET all’avvio della modalità webhook e un solo POST se il GET restituisce un elenco valido e l’URL manca. Su 409, errore o risposta incerta, il polling resta operativo e il prossimo tentativo riparte dal GET. La disattivazione rimuove la rotta locale. L’URL remoto può rimanere registrato e occupare uno slot, anche dopo un rollback o un cambio di dominio. I metodi di eliminazione o sostituzione globale restano esclusi.

## Ricezione e aggiornamento

- HMAC-SHA256 sul corpo grezzo con il client secret dell’app personale già configurata. Confronto a tempo costante con il valore esadecimale di `X-Netatmo-secret`. Firma assente o errata: HTTP 403. Un corpo firmato e valido riceve HTTP 200 senza attendere Netatmo o il coordinatore. Il corpo ha un limite di 256 KiB e un tempo di lettura massimo di 3 secondi. La compressione della richiesta è esclusa per preservare la verifica sui byte.
- Deduplica per SHA-256 dei byte ricevuti, in memoria fino a 15 minuti e 2.048 notifiche. La finestra riparte al ricaricamento. Una versione riserializzata dello stesso evento può eludere la deduplica, mentre l’accorpamento e il limite dei refresh continuano a operare.
- Le notifiche di attivazione e cancellazione sono contate. In modalità refresh, `device_event`, `home_event_changed` e `topology_changed` richiedono una lettura solo se il campo documentato `home_id` corrisponde a una casa già nota. Gli eventi sconosciuti o senza casa corrispondente restano visibili come contatori. Ordine, `sequence_id` e campi dei moduli non determinano lo stato delle entità. La sorgente di stato resta la coppia `homesdata` e `homestatus`.
- Una raffica si accorpa in un aggiornamento in attesa. Il limite di 60 secondi riguarda il refresh causato dai webhook. Polling, aggiornamenti manuali e comandi mantengono la cadenza preesistente e contano anch’essi nel consumo API. Ogni lettura effettua una richiesta per l’inventario e una per ogni casa Smarther individuata. In caso di HTTP 429 si riusa il cooldown della 0.2.1, da almeno 15 minuti e prolungabile tramite `Retry-After`. Il cooldown è locale alla voce, non un contatore globale delle richieste Netatmo.

## Diagnostica e limiti

- La diagnostica di Netatmo Enhanced contiene modalità, esito della registrazione e contatori aggregati. URL, ID, payload, firme e credenziali sono esclusi. I log di accesso del proxy e i log del componente webhook di Home Assistant possono includere il percorso. Condividi la diagnostica dell’integrazione al posto dei log completi.
- `invalid_external_https_url` indica che Home Assistant non ha fornito un URL esterno HTTPS utilizzabile. `get_http_401` o `post_http_401` indicano un problema di autorizzazione e `403` può dipendere dall’abilitazione dell’app. Un `post_http_409` lascia intatte le registrazioni esistenti. Dopo aver risolto la causa, ricarica la voce per ripetere il GET. Per un 429 attendi la scadenza del cooldown.
- La tabella Netatmo include BNS e BAC per `read_smarther`. Questa release conserva il rilevamento BNS della 0.2.1. Gli esempi forniti non documentano i campi specifici dei cambi di temperatura, modalità e programmazione di Smarther. La registrazione riuscita da sola non comprova la consegna di questi eventi.
- Netatmo indica una risposta entro 5 secondi, fino a tre ritentativi e sospensioni fino a 10 minuti dopo errori ripetuti. Gli eventi generati durante la sospensione possono andare persi. La convergenza dipende dal polling di riserva e dall’accessibilità dell’API.
- Verifica statica rispetto a Home Assistant Core 2026.11.0, con test offline su Python 3.12 e sostituti per le interfacce HA. Nessuna prova runtime su HA, chiamata autenticata a Netatmo o prova di consegna tramite tunnel è stata eseguita. La compatibilità su altre versioni di HA resta da verificare.

Test da eseguire dalla radice dell’archivio con `python -m unittest discover -s tests -v`.

Fonti pubbliche: [Netatmo Connect Webhooks](https://dev.netatmo.com/apidocumentation/webhooks), [endpoint pyatmo](https://github.com/jabesq-org/pyatmo/blob/development/src/pyatmo/const.py), [lettura delle registrazioni pyatmo](https://github.com/jabesq-org/pyatmo/blob/development/src/pyatmo/auth.py), [webhook HA Core 2026.11.0](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/webhook/__init__.py), [OAuth HA Core 2026.11.0](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/helpers/config_entry_oauth2_flow.py).
