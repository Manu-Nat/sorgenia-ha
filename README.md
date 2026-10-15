# Sorgenia Bills per Home Assistant

Integrazione community non ufficiale per Home Assistant. Versione 1.3.5.

## Installazione con HACS

1. In HACS apri il menu ⋮ → **Repository personalizzati**.
2. Inserisci l'URL GitHub di questo repository e scegli **Integration**.
3. Installa **Sorgenia Bills**, riavvia Home Assistant e aggiungi l'integrazione da **Impostazioni → Dispositivi e servizi**.
4. Prima del collegamento, crea `/config/sorgenia.yaml` partendo da `sorgenia.yaml.example` e compila i dati di configurazione in locale.

Questa integrazione non è affiliata a Sorgenia. Username e password vengono usati per login e OTP; la password inserita non viene conservata.

## Quale password inserire

Per il login automatico dell'integrazione, usa il valore del campo `password` inviato dal portale MySorgenia nella richiesta API `POST /sorgenia/V5/login`. Se questo valore è diverso dalla password digitata nel modulo del sito, inserisci nell'integrazione il valore del payload.

### Come trovare il valore nel browser

1. Apri la pagina di accesso MySorgenia, ma **non effettuare ancora il login**.
2. Premi **F12** per aprire gli strumenti per sviluppatori. In alternativa, fai clic destro sulla pagina e scegli **Ispeziona**.
3. Apri la scheda **Network** (in italiano può chiamarsi **Rete**). Se non la vedi, apri il menu `»` delle schede nascoste.
4. Facoltativamente, svuota l'elenco delle richieste con l'icona del cestino, così sarà più facile individuare quella corretta.
5. Lasciando aperta la scheda Network/Rete, inserisci username e password sul sito Sorgenia e avvia il login.
6. Nell'elenco delle richieste seleziona quella **POST** il cui indirizzo termina con `/sorgenia/V5/login`. Puoi usare il filtro e cercare `V5/login`.
7. Nel pannello dei dettagli apri **Payload** (o **Carico utile**) e individua il campo `password` nel corpo della richiesta.
8. Copia il valore di quel campo e inseriscilo nel campo password della configurazione Sorgenia in Home Assistant. Non copiare il token `accessToken` dalla risposta: non è la password.

Se non trovi la richiesta, controlla di aver aperto Network prima di inviare il login e di aver selezionato la richiesta POST corretta. Se il valore è oscurato o non compare, non provare a ricavarlo da altri campi.

Il valore `password` è una credenziale: non incollarlo in chat, issue GitHub o screenshot e non inserirlo in file pubblici. Anche il file `/config/sorgenia.yaml` contiene dati sensibili e non va pubblicato.

## Funzioni

Recupera bollette luce e gas, consumi fatturati, curve mensili per fascia e lettura gas configurata. Consulta il README per i dettagli dei campi, dei sensori e delle limitazioni.

### Rinnovo automatico del token

Il token di accesso Sorgenia dura circa 2 ore. Dalla versione 1.3.0 l'integrazione:

- salva il `refreshToken` ricevuto con l'OTP e lo invia a Sorgenia nell'header `refreshToken` (corpo: username e `sourceChannel`), che è il formato accettato dal servizio; se il `refreshToken` non è stato salvato (accesso fatto con una versione precedente) chiede subito di riautenticarsi;
- rinnova il token **10 minuti prima della scadenza**, senza aspettare il prossimo aggiornamento dei dati (che avviene ogni 6 ore), e salva il nuovo token;
- non ricarica più l'integrazione a ogni rinnovo;
- se Sorgenia rifiuta il rinnovo, mostra in Home Assistant la richiesta **Riautentica** (credenziali + OTP) invece di lasciare le entità non disponibili senza spiegazioni;
- in caso di rifiuto registra il codice e la descrizione dell'errore restituiti da Sorgenia (e-mail e stringhe lunghe mascherate);
- aggiunge il pulsante diagnostico **Refresh token now**, che prova subito il rinnovo senza aspettare 2 ore: l'esito si legge negli attributi di **Token expiry** (`refresh_error`, `refresh_method`, `last_refresh`), che resta visibile anche se gli altri sensori diventano non disponibili. L'attributo `integration_version` indica la versione realmente caricata da Home Assistant: se non corrisponde a quella installata, riavvia Home Assistant;
- aggiunge il sensore diagnostico **Token expiry**, con la scadenza del token e, negli attributi, ora e modalità dell'ultimo rinnovo (`last_refresh`, `refresh_method`) e l'ultimo errore (`refresh_error`).

Dopo l'aggiornamento serve **un'ultima autenticazione** (Riconfigura o Riautentica): solo così viene salvato il `refreshToken`. Il sensore **Token expiry** deve poi spostare la scadenza in avanti di circa 2 ore a ogni rinnovo.

### Dispositivo Sorgenia

Le entità sono raggruppate nel dispositivo **Sorgenia**: in **Impostazioni → Dispositivi e servizi → Sorgenia** compare il dispositivo con tutte le entità elencate. Gli `entity_id` esistenti non cambiano.

### Diagnostica del rinnovo token

L'integrazione registra l'esito del rinnovo e il codice d'errore API, senza mostrare token o credenziali. Per raccogliere dettagli se il problema si ripresenta, aggiungi temporaneamente questo blocco a `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.sorgenia: debug
```

Se `configuration.yaml` contiene già una sezione `logger:`, aggiungi solo la riga `custom_components.sorgenia: debug` sotto la sezione `logs:` esistente. Riavvia Home Assistant; dopo il successivo aggiornamento Sorgenia, cerca `Sorgenia` in **Impostazioni → Sistema → Registri**. Quando hai raccolto il log, rimuovi la riga di debug e riavvia di nuovo.
