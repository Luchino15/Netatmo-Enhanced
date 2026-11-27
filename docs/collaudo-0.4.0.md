# Collaudo Netatmo Enhanced 0.4.0

Procedura per Home Assistant OS 2026.11.3 su Raspberry Pi 4 ARM64, con BTicino Smarther BNS, app Netatmo Connect personale e webhook HTTPS già funzionante. Le prove sul termostato vanno eseguite dall’operatore dopo la pubblicazione della pre-release.

## Pubblicazione e installazione

1. Dopo la revisione e la CI verde, unisci la PR con l’autorizzazione del manutentore. Verifica la versione `0.4.0` nel manifest sul commit approvato di `main`.
2. In **GitHub → Releases → Draft a new release** crea il tag `v0.4.0` su quel commit, seleziona `v0.3.1` come versione precedente e usa le note del [changelog](../CHANGELOG.md). Attiva **Set as a pre-release** e conserva la bozza fino alla decisione di pubblicazione. Il Marketplace usa l’archivio sorgente GitHub. [Gestione delle release GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
3. In Home Assistant fai un backup. Dalla scheda di Netatmo Enhanced in **Impostazioni → Marketplace**, abilita **Pre-release** e aggiorna le informazioni del repository. Installa la 0.4.0 e riavvia Home Assistant mantenendo la voce esistente. [Aggiornamenti Marketplace](https://contributors.home-assistant.io/marketplace/guides/releases/).
4. Controlla che la modalità webhook iniziale sia quella usata con la 0.3.1. Confronta ID e storico delle entità, scheda dispositivo e disponibilità delle altre integrazioni. La configurazione con uno Smarther espone otto sensori, un’entità climate e un selettore di programmazione per la casa.

## Preparazione e misurazione

1. Lascia attivo il polling nella scheda dell’integrazione. Attendi che la temperatura ambiente, l’umidità, la stagione, la modalità, il setpoint e la programmazione coincidano con l’app o il display del termostato.
2. Dalla scheda della voce scarica la diagnostica. Verifica `webhook_v1.registration`. `registered`, `registered_verified` e `already_registered` indicano la presenza della registrazione. Una modifica reversibile dall’app deve produrre un incremento di `device_event` o `home_event_changed`.
3. Esegui un cambio di setpoint in **Ricezione e aggiornamento API** per avere un confronto sullo stesso impianto. Scarica la diagnostica prima e dopo. Ripristina il valore, poi seleziona **Aggiornamento diretto (Cloud Push)** in **Impostazioni → Dispositivi e servizi → Netatmo Enhanced → Configura**.
4. Esegui una prova alla volta. Per ciascuna annota in locale il canale del comando, ora di azione, ora di aggiornamento visibile, stato iniziale e finale, contatori prima e dopo e risultato. Attendi la stabilizzazione dello stato prima della prova successiva. In caso di raffiche e passaggi di stagione annota l’ordine effettivo delle transizioni nel termostato.
5. Per misurare la latenza interna usa `receipt_to_publication_ms` degli eventi diretti. Registra il numero di campioni, la mediana, il 95º percentile, il massimo e quanti superano 100 ms. Misura separatamente dall’orologio locale il tempo dal gesto nell’app o sul termostato fino alla variazione visibile in Home Assistant. Quella misura comprende la consegna, la rete e la UI.
6. Calcola le differenze di `netatmo_api.http_requests_total` e delle righe per origine. Una lettura per una casa può usare `homesdata` e `homestatus`. Distingui `webhook`, `periodic`, `command`, `startup` e `registration`. Per un push completo e non ambiguo l’incremento atteso per `webhook` è zero. Se cade un polling durante la prova, attribuiscilo a `periodic`.

La sessione reale di riferimento della 0.3.1 ha registrato 114 tentativi HTTP, 113 riusciti, un 503 e zero 429. I webhook hanno causato 99 richieste. I 74 campioni locali dalla ricezione alla pubblicazione hanno avuto mediana 23,408 s e massimo 62,873 s. I volumi assoluti dipendono da durata, attività e frequenza del polling. Per confrontare i due modi usa la stessa sequenza e il delta per origine.

## Prove sul termostato

Usa le condizioni normali dell’impianto. Ripristina le impostazioni originali dopo ogni gruppo. Effettua i cambi di stagione dall’app secondo i tempi di commutazione previsti dal termostato e dall’impianto.

| Prova | Operazione | Risultato da osservare |
|---|---|---|
| Setpoint dall’app | Cambia il setpoint di un passo, attendi la pubblicazione e ripristinalo. | Setpoint effettivo e modalità Manuale. Il ripristino produce una nuova transizione. |
| Setpoint dal termostato | Ripeti il cambio e il ripristino dal display fisico. | Gli stessi campi di Home Assistant cambiano al push corrispondente. |
| Setpoint da Home Assistant | Ripeti il cambio e il ripristino dall’entità climate. | La risposta del comando coincide con lo stato ricevuto. Confronta le letture `command` e `webhook` con la prova in modalità API. |
| Manuale → Programmazione | Imposta una modifica temporanea, poi scegli Programmazione da Home Assistant. | Modalità stanza `home`, programmazione attiva e cancellazione del termine manuale quando comunicato. |
| Scadenza Manuale | Imposta una modifica temporanea breve dall’app e attendi il termine. | Ritorno automatico e cancellazione del termine `*_setpoint_end_time` quando comunicata. |
| Assente | In riscaldamento seleziona Assente dall’app, attendi e torna a Programmazione. | Modalità globale `away`, anche se la stanza comunica `home`. |
| Antigelo | In riscaldamento attiva Antigelo, attendi e torna a Programmazione. | Modalità effettiva Antigelo quando la stanza comunica `hg`. Controlla il ritorno in Auto. |
| Programmazioni | Seleziona un altro programma già configurato nella stagione corrente e torna al primo. | Selezione corretta in entrambe le direzioni e coerenza tra sensori e selettore. |
| Programmazione appena creata | Se disponi di una programmazione di prova, selezionala subito dopo averla creata dall’app. | Se l’ID era sconosciuto, il selettore si allinea dopo una sola verifica API accorpata. L’inventario può essere già stato aggiornato da un precedente evento. |
| Stagione ripetuta | Passa a raffrescamento, torna a riscaldamento e ripeti il primo cambio. | Tutti i cambi legittimi sono visibili. Confronta eventuali verifiche API e gli eventi che arrivano durante il cambio. |
| Attività riscaldamento | Osserva un avvio e un arresto in stagione di riscaldamento. | L’azione climate segue `boiler_status` e `cooler_status` fino allo stato inattivo. |
| Attività raffrescamento | Osserva un avvio e un arresto in stagione di raffrescamento. | L’azione climate segue `cooler_status` e `boiler_status` fino allo stato inattivo. |
| Temperatura e umidità spontanee | Lascia funzionare il termostato senza comandi. | Aggiornamenti ambientali. Un evento di sola umidità mantiene invariati setpoint, temperatura e modalità già acquisiti. |
| Preriscaldamento | Osserva un anticipo del riscaldamento prima di un orario programmato. | Setpoint effettivo confermato dal termostato e azione di riscaldamento. Dopo un eventuale annullamento manuale confronta di nuovo il setpoint effettivo. |
| Polling sotto traffico push | Lascia attive le entità per almeno 20 minuti durante una normale sessione che generi notifiche. | `netatmo_api.coordinator_updates.periodic` aumenta anche quando si osservano aggiornamenti diretti tra due cicli. |
| Cambio modalità | Seleziona **Ricezione e aggiornamento API**, ripeti un cambio reversibile, poi riattiva Cloud Push. | Il primo evento usa la lettura API e il successivo può aggiornare direttamente. Entità e registrazione restano disponibili. |

## Interruzione e ripristino controllati

1. Con Cloud Push attivo, verifica una consegna e misura il prossimo ciclo di polling a partire dall’ultimo ciclo periodico. Salva la diagnostica iniziale.
2. Nella sola voce **Netatmo Enhanced**, passa a **Solo ricezione**. Questa modalità simula una perdita degli aggiornamenti push per le entità mentre Home Assistant continua a rispondere a Netatmo. Cloudflare Tunnel, registrazione remota, app OAuth e altre integrazioni continuano a operare.
3. Cambia il setpoint dall’app, lontano da un ciclo periodico atteso. Verifica la ricezione nella diagnostica e osserva lo stato in Home Assistant fino alla successiva lettura periodica. Confronta l’aumento delle richieste `periodic` e il nuovo valore confermato.
4. Riattiva **Aggiornamento diretto (Cloud Push)**. Ripeti il cambio dall’app verso il valore iniziale e verifica la pubblicazione diretta e l’assenza di letture `webhook` per l’evento completo. Questo controlla la ripresa con il medesimo URL e la stessa registrazione.
5. Se Netatmo Enhanced usa un’app Netatmo Connect dedicata alla sola integrazione, puoi provare anche l’assenza del gestore locale. Ripeti i passi 2–4 selezionando **Disattivato** al passo 2. Al passo 4 attendi il controllo della registrazione preesistente e osserva un nuovo evento. Con un’app OAuth condivisa usa **Solo ricezione**, così Netatmo continua a ricevere risposte dalla rotta. Conserva in locale gli eventuali log di Home Assistant, che possono contenere riferimenti alla rotta privata.

La prima variante isola il recupero delle entità. La seconda copre la rimozione e riattivazione del gestore locale. Ritardi e ritentativi durante una reale indisponibilità di Netatmo o della rete dipendono anche dall’infrastruttura del fornitore. Per le inversioni prive di una sequenza verificabile, una riconciliazione può precedere l’allineamento dello stato.

## Lettura della diagnostica

- `webhook_v1.recent_events` conserva fino a 100 eventi autenticati per la voce caricata. `processing_ms` misura l’elaborazione interna del webhook e `receipt_to_publication_ms` il tempo locale fino alla pubblicazione ai listener. `api_limit_wait_ms` e `command_lock_wait_ms` riguardano i cicli API. L’aggiornamento diretto pubblica senza attendere entrambi.
- `webhook_v1.events` include il numero di consegne `received` e i risultati del Cloud Push come `push_applied`, `push_partial_applied`, `push_unchanged`, `push_duplicate`, `push_obsolete`, `push_ambiguous` e `push_unrecognized`. Un evento con più campi può contribuire a più risultati. Le verifiche accodate e accorpate compaiono in `reconciliation_needed`, `reconciliation_reasons`, `refresh_pending`, `refresh_success` e `refresh_failed`.
- `webhook_v1.events.api_refresh_triggers_avoided` conta un innesco API evitato per ogni evento idoneo che modifica lo stato senza richiedere la riconciliazione. La diagnostica include il criterio in `api_refresh_triggers_avoided_criterion`. Poiché più notifiche possono corrispondere allo stesso ciclo API, per confrontare il traffico effettivo usa anche i delta HTTP per origine.
- `netatmo_api.http_requests` espone tentativi, codici HTTP, errori e durate per origine e percorso normalizzato. `coordinator_updates` conta i cicli di aggiornamento. La registrazione ha contatori separati e il rinnovo OAuth è gestito da Home Assistant. [Campi introdotti nella 0.3.1](collaudo-0.3.1.md#lettura-della-diagnostica).
- `cloud_push` contiene `last_push_at_utc`, `last_api_at_utc`, `last_api_outcome` e `last_api_failure_reason`, oltre a `poll_interval_seconds`, `poll_scheduled` e `poll_disabled_in_home_assistant`. `api_responses_with_protected_fields` conta le risposte in cui il coordinatore ha conservato campi push più recenti. `api_failures_retaining_data` conta i guasti temporanei durante la finestra in cui rimangono disponibili gli ultimi dati validi. Dopo 20 minuti dalla precedente lettura API riuscita un guasto prolungato può rendere indisponibili le entità.
- I contatori `commands_confirmed_by_push`, `command_api_verifications`, `commands_confirmed_by_api` e `commands_unconfirmed` distinguono l’esito delle verifiche dei comandi. Un comando può richiedere più di una verifica quando il ritorno alla programmazione ripristina sia la modalità della casa sia quella della stanza.
- Gli identificativi sono pseudonimizzati e i campi liberi sono oscurati. Tempi, valori ambientali e profili d’uso possono comunque identificare abitudini. Conserva la diagnostica completa in locale. Per una segnalazione condividi soltanto conteggi e latenze aggregate, oppure un caso sintetico che mantenga la struttura dell’evento.

## Rollback

1. Per tornare alla strategia precedente nella 0.4.0, scegli **Ricezione e aggiornamento API** dalle opzioni e attendi l’esito della prossima lettura. Verifica che aumenti `webhook_v1.refresh_success` dopo una modifica dall’app.
2. Per ripristinare i file, con quella modalità già selezionata installa `v0.3.1` dal menu delle versioni Marketplace. Per un’installazione manuale sostituisci la sola cartella `/config/custom_components/netatmo_enhanced/` con quella dell’archivio `v0.3.1`.
3. Riavvia Home Assistant, mantieni la voce OAuth esistente e verifica entità, storico, registrazione e comportamento dopo un evento. Il ripristino usa lo stesso ID e lascia attiva la registrazione Netatmo preesistente.
