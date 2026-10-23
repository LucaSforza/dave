# Valutazione cluster per DAVE / RexROV e PPO

**Rilevazione:** 23 ottobre 2026, via `ssh cluster`. Nessuna configurazione del cluster è stata modificata. Nessuna simulazione o benchmark è stato eseguito sul login node.

## 1. Risorse e policy osservate

### Login node `submitter`

- Ubuntu 24.04.2 LTS, x86_64; kernel 6.8.
- CPU visibili: 2 × AMD EPYC 7301, 32 core fisici / 64 thread.
- Memoria visibile: circa 251 GiB; è la memoria del nodo condiviso, non una quota assegnata a un job.
- Software: Docker 28.0.2 con daemon accessibile all'account; SingularityCE 3.9.8 con supporto `--nv`.
- `nvidia-smi`, `nvcc`, ROS, Gazebo e il comando `module` non sono disponibili sul login node.
- Il login non è stato usato per eseguire simulazioni, benchmark o training.

### Slurm

- Partizione visibile: `students` (31 nodi; tutti i nodi elencati appartengono a questa partizione).
- Limiti della partizione: massimo 30 minuti/job, 4 nodi/job, 62 CPU/nodo; QoS predefinita `students_limit`.
- Associazione dell'account: QoS `normal,students_limit`; la partizione `students` consente `students_limit`.
- `students_limit`: `MaxJobs=1`, `MaxSubmit=1`.
- Nodi GPU dichiarati da Slurm: `quadro_rtx_6000`, uno o due per nodo. Il modello e la VRAM non sono stati verificati con `nvidia-smi` sul nodo.
- Configurazione riportata per `node122`: 64 thread CPU, 257566 MiB RAM, una Quadro RTX 6000; nodo condiviso e occupato al momento della verifica.
- Al momento del controllo nessun nodo GPU risultava `idle`. Una richiesta di sola ispezione, limitata a 2 minuti / 1 GPU / 4 CPU / 8 GiB, è stata rifiutata con `Requested nodes are busy`; `squeue` successivo non mostrava job dell'utente.
- Nessun modulo software o documento di policy è stato individuato nei controlli eseguiti. La policy effettivamente osservabile è quella di Slurm sopra riportata; verificare con l'amministratore eventuali regole aggiuntive sull'uso di Docker, Singularity e GPU.

## 2. Compatibilità con l'ambiente DAVE

- L'immagine locale `localhost/dave-ros-noetic:local` è amd64 e misura circa 4,86 GB. Contiene ROS Noetic, Gazebo Classic e il plugin `libgazebo_ros_velodyne_gpu_laser.so` richiesto dal LiDAR SL3.
- Docker è presente sul login node, ma non ho trasferito né avviato l'immagine sul cluster. SingularityCE supporta `--nv`, quindi il percorso OCI → SIF è plausibile; la conversione va effettuata in uno spazio di calcolo consentito e con spazio disco sufficiente, non lanciata sul login.
- `GUI=false` evita il client grafico, ma **non prova** che il rendering dei sensori GPU funzioni sul cluster. Servono driver NVIDIA compatibili esposti nel container e un contesto di rendering headless (EGL/OGRE o configurazione supportata dalla build Gazebo). `singularity exec --nv` da solo non è garanzia.
- Sul PC locale, il demo headless ha pubblicato PointCloud2 SL3 e immagini RGB valide con rendering software. È una verifica positiva del modello e dei topic, non una verifica del percorso GPU NVIDIA né delle prestazioni del cluster.
- ROS Noetic e Gazebo Classic sono EOL; non è stata fatta alcuna migrazione ROS 2 e l'ambiente Docker locale è rimasto intatto.

## 3. Benchmark e training

**Non eseguiti.** Non è stata ottenuta un'allocazione GPU, quindi non sono stati avviati RexROV, LiDAR/camere, benchmark RTF o training sul cluster. Non esistono dati misurati per confrontare il PC locale con il cluster o per stimare il numero di simulatori paralleli.

La GPU può essere utile al training PPO, ma la simulazione Gazebo può essere limitata da CPU, rendering e memoria prima che lo sia l'update PPO. Il limite di 30 minuti per job e `MaxJobs=1` rendono inoltre necessario checkpoint/resume e possono essere scomodi per training prolungati. Raccomandazione provvisoria: mantenere il container locale come riferimento riproducibile; valutare il cluster per rollout/training solo dopo un test GPU headless allocato e un benchmark breve. Per PPO lungo, chiedere se esiste una QoS/partizione con wall-time maggiore.

## Prossimo passo

Quando una GPU sarà disponibile, richiedere un terminale allocato (la richiesta resta soggetta a coda e policy):

```bash
ssh cluster 'srun --partition=students --gres=gpu:1 --cpus-per-task=4 --mem=16G --time=00:10:00 --pty bash -l'
```

Dentro l'allocazione si potrà verificare `nvidia-smi`, driver e rendering headless; solo dopo, trasferire/convertire l'immagine e lanciare un smoke test limitato per LiDAR, camera e RTF. Non lanciare il test sul login node.
