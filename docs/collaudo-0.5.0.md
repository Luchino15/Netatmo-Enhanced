# Collaudo Netatmo Enhanced 0.5.0

Procedura per BTicino Smarther BNS su Raspberry Pi 4 ARM64, Home Assistant OS 18.5 e Core 2026.11.3. I test automatici usano dati sintetici e un trasporto simulato. Le verifiche in questa pagina richiedono il termostato e vanno eseguite dall’operatore dopo l’installazione della pre-release.

## Pubblicazione e installazione

1. Dopo la revisione e la CI verde, il manutentore approva il merge e verifica il manifest `0.5.0` sul commit di destinazione di `main`.
2. In **GitHub → Releases → Draft a new release**, crea il tag `v0.5.0` sul commit approvato. Seleziona `v0.4.0` come versione precedente, copia le note del changelog e attiva **Set as a pre-release**. Salva la bozza fino alla decisione di pubblicazione. [Gestione delle release GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
3. Fai un backup di Home Assistant. Nella scheda del repository già installato in **Impostazioni → Marketplace**, abilita **Pre-release**, aggiorna le informazioni del repository e installa la 0.5.0. Riavvia Home Assistant mantenendo la voce Netatmo Enhanced già configurata. [Aggiornamenti Marketplace](https://contributors.home-assistant.io/marketplace/guides/releases/).
4. Verifica la modalità webhook già impostata, gli ID e lo storico delle entità esistenti, la scheda del dispositivo e la presenza della registrazione webhook. Il valore `webhook_v1.registration` può risultare `already_registered`, `registered` o `registered_verified`. Controlla che le altre integrazioni Netatmo siano operative.

## Preparazione e misurazione

1. Usa le condizioni normali dell’impianto. Annota la stagione, la modalità della casa e della stanza, il programma attivo e il setpoint iniziale. Inizia con il riscaldamento in Programmazione e il polling di Home Assistant abilitato.
2. Dalla scheda della voce scarica la diagnostica prima e dopo ogni gruppo di prove. Annota in locale il canale usato, l’azione, l’ordine degli eventi, l’ora del comando e l’ora dell’aggiornamento visibile. Attendi che l’app, il display e Home Assistant concordino prima di proseguire.
3. Misura `webhook_v1.recent_events[].receipt_to_publication_ms` per gli aggiornamenti diretti e riporta numero di campioni, mediana, 95º percentile e massimo. Confronta a parte il tempo dal gesto fino allo stato visibile in Home Assistant, che include rete, consegna Netatmo e interfaccia.
4. Confronta i delta di `netatmo_api.http_requests_total` e delle righe `netatmo_api.http_requests` per origine. Separa il comando di scrittura e le letture `command` di verifica dalle riconciliazioni `webhook` e dai cicli `periodic`. Un ciclo API con una sola casa usa normalmente una richiesta di inventario e una di stato. Controlla `cloud_push.commands_confirmed_by_push` e `cloud_push.command_api_verifications` per individuare il percorso seguito.
5. Esegui prima le prove in **Aggiornamento diretto (Cloud Push)**. Ripeti un singolo caso reversibile in **Ricezione e aggiornamento API**, poi ripristina la modalità iniziale. Conserva la diagnostica completa in locale e condividi soltanto i conteggi e le latenze aggregate o fixture sintetiche. [Campi e cautele della diagnostica](collaudo-0.4.0.md#lettura-della-diagnostica).

`cloud_push.controls` contiene contatori e fino a 40 eventi relativi ai comandi e alle transizioni di stato. Usa `recent` per vedere l’avvio, il cambio di scadenza o la fine di Boost, oltre a stagione e modalità stanza dove rilevanti. L’evento `boost_return_after_deadline` indica che il ritorno è stato osservato dopo la scadenza attesa. Confrontalo con le azioni annotate per distinguere la scadenza naturale da un intervento manuale.

Nel collaudo precedente della 0.4.0, la diagnostica finale contava 318 consegne, 259 aggiornamenti diretti e 240 inneschi di un refresh API evitati. Sugli 84 campioni disponibili la latenza interna aveva mediana 3,814 ms e massimo 6,228 ms. Sono stati osservati 229 tentativi HTTP, con 226 successi e tre risposte 503. I totali dipendono dalla durata del collaudo e dai programmi attivi. Usa i delta per ciascuna prova.

## Prove Boost

Per ogni comando osserva la durata effettiva comunicata dal termostato, la modalità e l’azione climate. Usa le prove nell’ordine indicato e ripristina il programma iniziale dopo ogni gruppo.

| Prova | Operazione | Risultato da osservare |
|---|---|---|
| App e scadenza naturale | Avvia Boost da Home + Control e lascia terminare il ciclo da 30 minuti. | Home Assistant converge sullo stato effettivo. Alla scadenza controlla il ritorno alla modalità applicabile e la rimozione delle informazioni relative al Boost terminato. |
| Termostato e arresto | Avvia Boost dal termostato e verifica i passaggi disponibili sul comando fisico. Interrompi Boost prima della scadenza. | Acquisizione delle transizioni e dell’arresto da parte di Home Assistant. Controlla il ritorno alla modalità applicabile e il setpoint effettivo. |
| Home Assistant, durata scelta | Dall’azione **Imposta Boost** prova 30, 60 e 90 minuti, uno alla volta. Usa **Interrompi Boost** prima di avviare il successivo. | La durata corrisponde alla richiesta e la conferma compare dopo un evento pertinente o una lettura API. |
| Home Assistant, durata predefinita | Seleziona il preset **Boost** dell’entità climate. | Il sensore di scadenza indica circa 90 minuti dalla partenza. Verifica la corrispondenza con l’app. |
| Home Assistant, ciclo durante un Boost | Senza Boost attivo, usa **Cambia durata Boost** più volte e attendi la conferma tra un’azione e l’altra. | Il ciclo segue 30, 60, 90 minuti e arresto. Osserva l’aggiornamento della scadenza. |
| Home Assistant riavviato a Boost attivo | Avvia Boost da un canale esterno, attendi parte del ciclo e riavvia la voce. Prova il ciclo se la durata iniziale è ancora sconosciuta a Home Assistant. | In quel caso Home Assistant chiede una durata esplicita o l’arresto. Usa **Imposta Boost** con una durata scelta, confronta la nuova scadenza e termina la prova con **Interrompi Boost**. |
| Boost in raffrescamento | Dopo il cambio stagione, ripeti da app e Home Assistant un ciclo breve e il relativo arresto. | Verifica lo stato di raffrescamento, la scadenza e il ritorno alla programmazione. Questa scrittura è sperimentale per BNS. |

Per visualizzare insieme l’ora di fine e il tempo residuo, usa due righe della stessa entità timestamp in una scheda **Entità**. Sostituisci l’ID di esempio con quello del sensore **Scadenza regolazione temporanea**. La visualizzazione relativa si aggiorna nell’interfaccia senza richieste a Netatmo. [Formati data e ora della scheda Entità](https://www.home-assistant.io/dashboards/entities/#yaml-configuration).

```yaml
type: entities
entities:
  - entity: sensor.smarther_scadenza_regolazione_temporanea
    name: Scadenza effettiva
    time_format: time
  - entity: sensor.smarther_scadenza_regolazione_temporanea
    name: Tempo residuo
    time_format: relative
```

Esegui un caso senza consegna utile al coordinatore con la modalità **Solo ricezione**. Usa un comando reversibile, verifica la conferma tramite API e ripristina Cloud Push. Il tunnel e la rotta HTTPS possono restare in servizio. [Prova di interruzione della 0.4.0](collaudo-0.4.0.md#interruzione-e-ripristino-controllati).

## Stagioni, modalità e regolazione manuale

Effettua i cambi di stagione rispettando i tempi di commutazione previsti dall’impianto. Attendi la conferma di un comando prima del successivo.

| Prova | Operazione | Risultato da osservare |
|---|---|---|
| Cambio stagione | Da Home + Control passa da riscaldamento a raffrescamento e ritorna alla stagione iniziale. | Stagione, setpoint effettivo e programmazioni disponibili concordano fra app e Home Assistant. |
| Programmazione estiva | Seleziona dall’entità di programmazione una seconda programmazione estiva già presente, quindi ripristina la prima. | Il selettore usa le opzioni della stagione attiva. Lo stato confermato coincide con quello dell’app dopo entrambi i setpoint programmati. |
| Auto e Assente | Attiva Assente dal controllo Home Assistant in riscaldamento e dall’app per l’estate, quando disponibile sul dispositivo. Ripristina dall’app la modalità globale eventualmente modificata. | La modalità globale della casa resta distinta da quella del termostato. Verifica l’effettivo ripristino della programmazione. |
| Spegnimento e Auto | In raffrescamento lascia la casa in Programmazione, seleziona **Spento**, attendi la conferma, poi scegli **Auto**. | Confronta modalità, azione del modulo e programma al ripristino. In riscaldamento usa Antigelo per la prova dedicata. |
| Manuale con durata | Usa **Imposta temperatura manuale con durata** per un cambio reversibile, con una durata breve da 1 a 720 minuti. Attendi la scadenza in entrambe le stagioni. Ripeti con interruzione anticipata e ritorno alla programmazione. | Confronta valore e scadenza confermati in app e Home Assistant. Dopo il termine verifica il ripristino. |
| Manuale ordinario | Cambia il setpoint con il controllo standard di Home Assistant e torna alla programmazione. | La regolazione ordinaria e il ritorno automatico seguono il comportamento precedente. |
| Antigelo | In riscaldamento attiva Antigelo e torna a Programmazione. | Modalità effettiva Antigelo e successivo ritorno in Auto. |

## Regressioni e ripristino

1. Ripeti almeno un cambio setpoint da iPhone, Home Assistant e display, un cambio programma in entrambe le direzioni e variazioni spontanee di temperatura e umidità. Verifica un avvio e un arresto dell’impianto in ogni stagione e, se osservabile nella normale programmazione, un evento di preriscaldamento. [Piano completo della 0.4.0](collaudo-0.4.0.md#prove-sul-termostato).
2. Verifica almeno due cicli `periodic` durante una sessione Cloud Push e un ripristino dopo **Solo ricezione**. I cicli continuano anche in presenza di notifiche frequenti. Confronta i delta API prima e dopo il cambio di modalità.
3. Al termine chiudi gli override in corso, ripristina la stagione e la programmazione iniziali e controlla il risultato sul termostato.

## Rollback alla 0.4.0

1. Chiudi il Boost o la regolazione temporanea dall’app o dal termostato, poi verifica la modalità effettiva.
2. Nella scheda Netatmo Enhanced in **Impostazioni → Marketplace**, seleziona `v0.4.0` dal menu delle versioni e riavvia Home Assistant mantenendo la voce già configurata. Per un’installazione manuale, sostituisci la sola cartella `/config/custom_components/netatmo_enhanced/` con quella dell’archivio `v0.4.0`.
3. Verifica OAuth, registrazione webhook, ID, storico ed entità della 0.4.0. Controlla la modalità webhook e ripeti una variazione dall’app e il normale polling. Le entità introdotte nella 0.5.0 possono restare nel registro fino alla successiva installazione di quella versione.
