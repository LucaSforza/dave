# Guida ai concetti e ai primi esperimenti

## Che cosa gira dove?

| Componente | Dove | A cosa serve |
|---|---|---|
| API FastAPI e worker | Docker sul PC | Ricevono richieste e coordinano il cluster |
| Redis | Docker sul PC, volume persistente | Ricorda sessioni, richieste e stato osservato |
| Slurm | Cluster | Decide quando e dove eseguire il lavoro |
| ROS, Gazebo e desktop | Container sul nodo di calcolo allocato | Eseguono fisica, sensori e interfaccia originale |
| Browser noVNC | PC | Mostra il desktop remoto attraverso SSH |

Un **job logico** è la sessione creata nell'API. Un'**allocazione Slurm** è una sua esecuzione temporanea sul cluster. La sessione può sopravvivere a una singola allocazione, ma il desktop no: occorre riconnettersi dopo un rinnovo.

`pending` non indica un errore: può significare attesa in coda. Il job deve essere dichiarato interattivo soltanto quando il runtime ha pubblicato la readiness, non appena Slurm gli assegna un nodo.

## Percorso iniziale consigliato

1. **Test senza cluster:** usare la suite locale con Slurm simulato. Verifica il software, non la GPU o il desktop remoto.
2. **Configurazione e preflight:** predisporre l'alias SSH nel container del backend, verificare l'host noto e configurare il runtime remoto e l'immagine completa. Il preflight non avvia Gazebo.
3. **Prima sessione breve:** creare esplicitamente una sessione, osservare la coda e i log, poi aprire il desktop quando pronto. Non confondere il successo della submission con il successo della simulazione.
4. **Esplorazione:** osservare RexROV in Gazebo, usare pausa/ripresa, provare la teleoperazione da tastiera con piccoli comandi e arresto automatico. Non è necessario un joystick fisico.
5. **Checkpoint:** salvare lo stato e ispezionare il manifesto. È un ripristino applicativo parziale del robot, non un'immagine della memoria dell'intero simulatore.
6. **Rinnovo controllato:** dopo un test singolo riuscito, provare al massimo due allocazioni. Verificare lo stato ripristinato e il nuovo endpoint desktop.
7. **Arresto:** annullare dall'API e verificare che Slurm confermi l'uscita. Chiudere soltanto il browser non annulla la sessione.

## Cosa osservare

- Il tempo di parete è quello del cluster; il tempo simulato può avanzare più lentamente o più velocemente.
- Una camera o un LiDAR configurato come GPU non prova che il rendering stia usando NVIDIA: verificare renderer e driver nell'allocazione.
- Un checkpoint è utilizzabile solo se completo, compatibile e verificato. Non rinominare file incompleti per forzare la ripresa.
- Se il PC si spegne, il salvataggio anticipato sul nodo deve ancora funzionare. Il successore verrà richiesto soltanto quando il controllore torna disponibile, non magicamente dal cluster.
- I rinnovi rispettano coda, quota e limite configurato; non garantiscono continuità e non aggirano il walltime.

Per comandi esatti di installazione, client e desktop consultare i README dei componenti. Annotare i risultati dei primi test reali in `kb/implementation.md`.
