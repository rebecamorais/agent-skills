# Capturing a heap snapshot

## Warnings (staging and production)

- **Pause and extra memory:** a snapshot **pauses the event loop** and may need ~1–2× the current
  heap on top. Capture early, while the heap is still small, and check that
  `limit − working_set` can take it.
- **Disk:** the file is roughly the size of the heap and goes to the node's disk (ephemeral
  storage). Check with `df -h /tmp`.
- **Taking the pod out of the Service (API):** first see what the Service selects on
  (`kubectl -n <ns> get svc <svc> -o jsonpath='{.spec.selector}'`). Removing one of those labels
  (`kubectl -n <ns> label pod <pod> <label>-`) stops traffic to the pod. If the ReplicaSet selects on
  the same label, the pod is also orphaned and a replacement starts; delete the orphan at the end.
  The liveness probe is still active, so a long pause can still restart the container.
- **Consumers and workers** (Kinesis, SQS, Kafka, RabbitMQ, cron): **don't remove the label**. They
  don't get work through the Service, so consumption and the leak go on, and the replacement pod
  competes for leases, partitions and messages with the isolated one. Prefer staging. The snapshot
  pause also stops consumption and can miss lease renewal and checkpoints (Kinesis), the session
  timeout (Kafka) or the visibility timeout (SQS).

## Staging: live DevTools

1. Turn on the inspector without restarting (keeps the leaked state). It listens on `127.0.0.1:9229` only:
   ```bash
   kubectl -n <ns> exec <pod> -c <container> -- sh -c 'kill -USR1 1'
   ```
   - If node is not PID 1 (`npm start`, `dumb-init`), find its PID by reading `/proc/*/comm`
     (not `ps aux`, which shows arguments).
   - Image without `sh`/`kill`: `kubectl debug -it <pod> --image=busybox --target=<container> -- kill -USR1 1`.
   - Restarting with `--inspect` also works, but always `--inspect=127.0.0.1:9229`, never
     `0.0.0.0`. Never expose 9229 through a Service or Ingress.
2. Port-forward:
   ```bash
   kubectl -n <ns> port-forward pod/<pod> 9229:9229
   ```
   If local port 9229 is taken, use `9230:9229` and add `localhost:9230` under *Configure…*.
3. In `chrome://inspect`, click **inspect** and open the **Memory** tab. How to read it is in
   [devtools-and-fixes.md](devtools-and-fixes.md).

## Production: snapshot by signal, with approval

**Before handing over the commands:**
- The downloaded file goes to `~/heapsnapshots/` and only the user opens it (DevTools). The agent never reads it.
- Ask for the link to the tech lead's approval recorded in writing (ticket or incident thread).
  "The tech lead said it's fine" is not enough. The approval covers this path, never the inspector.
- The commands are handed to the user to run; the agent does not run them.
- Make it clear that this path is **slow**: it needs a PR and a deploy, the deploy restarts the
  pods (resetting the leaked state) and then it takes hours for the leak to grow again. Rollback +
  reproducing in staging is usually faster.

1. **Flags.** Through a PR to the production manifest, add to the Node args **before** the
   entrypoint (or to `NODE_OPTIONS`):
   ```yaml
   args: ["--max-old-space-size=<75% of the limit>", "--heapsnapshot-signal=SIGUSR2", "--diagnostic-dir=/tmp", "<entrypoint>"]
   ```
   - Without this flag, `kill -USR2` **produces no snapshot**: with node as PID 1 the signal is
     ignored; with node behind `npm`/`dumb-init`, the process dies.
   - Check the args before sending the signal:
     `kubectl -n <ns> get pod <pod> -o jsonpath='{.spec.containers[?(@.name=="<container>")].args}'`.
   - Don't rely on `--heapsnapshot-near-heap-limit` unless `--diagnostic-dir` points to a volume
     that survives the restart: the dump is written at the moment of death and is lost with the
     container's filesystem. `/tmp` works only if the root filesystem is writable
     (no `readOnlyRootFilesystem`) or `/tmp` is an `emptyDir`.
2. **Capture.** Wait for the floor to rise (hours) and capture **before** getting close to the limit.
   - Out of the Service, the pod stops getting traffic and the leak stops growing. So, to compare,
     capture **two pods of different ages**, each taken out of the Service at its capture time.
   - With a single snapshot, use the *Summary* view sorted by Retained Size.
   ```bash
   kubectl -n <ns> label pod <pod> <service-selector-label>-   # API only; never a consumer
   kubectl -n <ns> exec <pod> -c <container> -- sh -c 'kill -USR2 1'
   kubectl -n <ns> exec <pod> -c <container> -- ls -la /tmp          # wait until the .heapsnapshot stops growing
   kubectl -n <ns> exec <pod> -c <container> -- cat /tmp/<file>.heapsnapshot > ~/heapsnapshots/<file>.heapsnapshot
   ```
   `cat` because `kubectl cp` needs `tar` in the image.
3. **Analysis.** In DevTools, **Memory → Load** the files and use the **Comparison** view.
4. **Cleanup.** `kubectl -n <ns> delete pod <pod>`, revert the args through a PR and delete the
   local files.
