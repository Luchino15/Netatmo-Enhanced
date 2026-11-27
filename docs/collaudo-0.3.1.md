# Netatmo Enhanced 0.3.1

## Pubblicazione e aggiornamento

1. Dopo CI verde e autorizzazione al merge, unisci la PR in `main` e verifica che il commit di destinazione contenga il manifest `0.3.1`.
2. In GitHub apri **Releases → Draft a new release**, crea il tag `v0.3.1` e seleziona il commit approvato su `main`. Indica `v0.3.0` come versione precedente e copia le note della 0.3.1 da [CHANGELOG.md](../CHANGELOG.md). La struttura del repository permette al Marketplace di usare l'archivio sorgente generato da GitHub.
3. Per distribuire la versione soltanto a chi partecipa al collaudo, seleziona **Set as a pre-release**. Salva come bozza fino all’autorizzazione alla pubblicazione, poi seleziona **Publish release**. La stessa release può essere promossa dopo il collaudo togliendo l'opzione di prerelease. [Procedura GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
4. In Home Assistant, fai un backup e apri la scheda del repository già installato in **Impostazioni → Marketplace**. Per una prerelease abilita **Pre-release** e aggiorna le informazioni del repository dalla scheda. Quando compare la 0.3.1, installala dalla scheda o da **Impostazioni → Aggiornamenti**. Riavvia Home Assistant mantenendo la voce dell'integrazione esistente. [Aggiornamenti Marketplace](https://contributors.home-assistant.io/marketplace/guides/releases/).
5. Per un ripristino, seleziona la versione `v0.3.0` dal menu delle versioni del repository nel Marketplace. Riavvia e mantieni la stessa voce di integrazione.

## Piano di collaudo su Home Assistant

Prima delle prove verifica che la voce sia in **Ricezione e aggiornamento API**, che le dieci entità siano presenti e che lo storico sia consultabile. Salva una diagnostica di riferimento dopo la registrazione. Annota per ogni prova l'ora UTC che osservi, il comando, il canale usato, lo stato prima e dopo e il risultato in Home Assistant. Ripristina i valori iniziali al termine di ogni gruppo.

| Prova | Come eseguirla | Cosa confrontare |
|---|---|---|
| Setpoint consecutivi | Da iPhone imposta due variazioni di 0,5 °C ravvicinate. Esegui poi una terza variazione dopo almeno 60 secondi dall'ultimo aggiornamento API. | Eventi, accorpamento, attesa minima, setpoint pubblicato. |
| Programmazioni | Passa da Inverno a Inverno Test e torna a Inverno. | Tipo evento, ID programma pseudonimizzato, stato confermato. |
| Manuale | Attiva una modifica temporanea e torna a Programmazione. | Modalità stanza, setpoint e campi di fine o durata eventualmente presenti. |
| Assente e Antigelo | In riscaldamento attiva e disattiva ciascun preset, tornando a Programmazione dopo ogni prova. | Modalità casa o stanza, eventi distinti, ritorno confermato. |
| Riscaldamento e raffrescamento | Usa l’app per cambiare stagione e osserva un avvio e un arresto per ogni stagione, compatibilmente con il normale uso dell’impianto. | Modalità stagionale, campi di richiesta e stato operativo, eventuale differenza tra inizio e fine. |
| Letture spontanee | In una sessione senza comandi, osserva variazioni naturali di temperatura ambiente e umidità. | Campi ricevuti, eventi assenti o presenti, eventuale aggiornamento periodico. |
| Confronto dei canali | Ripeti una variazione reversibile di 0,5 °C da Home Assistant, iPhone e termostato, a distanza di almeno un minuto l'una dall'altra. | Tipo e struttura degli eventi, latenza locale, richieste attribuite a `command` o `webhook`. |
| Polling di riserva | Lascia trascorrere un intervallo ordinario di 10 minuti con entità attive e senza nuovi eventi rilevanti. | Aumento di `coordinator_updates.periodic` e righe HTTP `periodic`. |

Scarica una nuova diagnostica alla fine di ogni gruppo, prima che il buffer sovrascriva gli eventi più vecchi. Conservala localmente. Per progettare la 0.4.0, confronta più prove prima di associare un campo a un'azione. Un campo assente in una consegna può comparire in un tipo di evento diverso. Tieni i dati diagnostici personali fuori da issue, PR e allegati GitHub.

## Lettura della diagnostica

- `webhook_v1.recent_events` contiene gli ultimi 100 eventi autenticati alla voce caricata. Comprende eventi validi, duplicati noti, eventi sconosciuti e messaggi firmati malformati. I rifiuti della firma restano nei contatori. I timestamp comprendono data UTC ISO 8601, timestamp Unix UTC e tempo monotono locale.
- La proiezione usa un elenco esplicito di contenitori, identificativi, campi tecnici ed enumerazioni. I valori identificativi ricevono pseudonimi coerenti nella stessa sessione. Nomi delle chiavi sconosciute e identificativi hanno pseudonimi indipendenti. I campi sconosciuti conservano forma e tipo entro limiti di profondità, ampiezza e nodi. Le stringhe libere, i valori sconosciuti, le firme e i payload originali restano fuori dall’export. La chiave casuale e il buffer vivono in memoria e si azzerano al ricaricamento. Orari e valori tecnici possono comunque rivelare abitudini di utilizzo.
- `refresh` indica se l'evento è accodato, accorpato, duplicato, in sola ricezione o privo di una casa corrispondente. `processing_ms` copre lettura del corpo, verifica, classificazione e proiezione. `api_limit_wait_ms` misura le porzioni di attesa già concluse dovute al limite minimo o al cooldown 429. `command_lock_wait_ms` misura l'attesa del blocco condiviso con i comandi.
- Gli eventi accorpati condividono `update`, con avvio e completamento del ciclo, istante della prima richiesta HTTP, tempo HTTP complessivo e durata del ciclo. `receipt_to_publication_ms` è calcolato per ogni evento dopo il completamento riuscito del coordinatore e delle notifiche ai listener. Misura la latenza locale dalla ricezione a Home Assistant, senza includere il tempo trascorso nell'app, in Netatmo, sul tunnel prima della ricezione o nel browser che mostra lo stato.
- `netatmo_api.http_requests` aggrega tentativi HTTP osservati dal client per origine, metodo e endpoint normalizzato, con status, errori, 429 e durate. Il totale include errori di rete e timeout e si riferisce alle richieste avviate direttamente dall'integrazione. Rinnovi OAuth gestiti da Home Assistant, altre integrazioni e blocchi preventivi del cooldown restano fuori dal totale. Un HTTP decodificabile può comunque essere rifiutato nel ciclo se la struttura non corrisponde a quella attesa.
- `coordinator_updates` conta cicli effettivi per `startup`, `periodic`, `manual`, `webhook` e `command`. Il conteggio `command` include le letture pre e post comando. Un ciclo riuscito può usare più richieste HTTP.
- `registration_details` descrive il codice HTTP del POST e un esito sintetico della risposta. `registered_verified` indica la conferma dal GET successivo. `post_unconfirmed` indica che neppure quel GET ha trovato l'URL. Gli esiti `verify_*` indicano una verifica impossibile o inattesa. Il comportamento osservato sulla 0.3.0 è compatibile con un POST accettato che non restituisce l'URL nel formato atteso, oppure con una conferma visibile soltanto in seguito. Senza il corpo della risposta originale la causa esatta resta aperta.
