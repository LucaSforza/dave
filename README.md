# DAVE

All documentation of Project DAVE can be found in the [Dave Documentation (https://field-robotics-lab.github.io/dave.doc/)](https://field-robotics-lab.github.io/dave.doc/).

The documentation source code is in the [Field-Robotics-Lab/dave.doc](https://github.com/Field-Robotics-Lab/dave.doc) repository.

## Controllo remoto del cluster (progetto locale)

- [Knowledge base e diagrammi UML Mermaid](kb/README.md)
- [Backend FastAPI/Redis con Docker e uv](backend/README.md)
- [Runtime Slurm e desktop Gazebo remoto](cluster/README.md)
- [Guida ai primi esperimenti](kb/learning-guide.md)

Il backend gestisce sessioni RexROV tramite l'alias SSH `cluster`. Il desktop
remoto usa l'interfaccia originale di Gazebo, senza un renderer personalizzato.
Il ripristino tra allocazioni è applicativo e parziale, non una copia completa
della memoria del simulatore; il collaudo reale sul cluster resta un passo
separato dai test locali.

`just run-cluster` avvia il backend locale, verifica i prerequisiti remoti e
richiede/riutilizza una sessione RexROV; `just run-cluster --desktop` attende e
apre il desktop. Runtime e SIF sul cluster devono essere già predisposti.

```bash
just backend-test
cp backend/.env.example backend/.env  # configurare UID/GID e SSH prima dell'avvio
just backend-up
```

L'API è disponibile su `http://127.0.0.1:8000/docs`. Prima di creare una sessione
remota occorre predisporre il runtime e l'immagine completa seguendo il README
del cluster. I comandi `just setup`, `just run` e `just stop` mantengono il loro
significato per la simulazione locale.
