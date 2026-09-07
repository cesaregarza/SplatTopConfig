# Citrus Redis persistence

The Citrus Redis Deployment stores its data on one retained 1 GiB
`ReadWriteOnce` PVC named `citrus-redis-data` in each namespace. The PVC uses
the `do-block-storage-retain` StorageClass and has both Helm keep and Argo CD
prune/delete protection. Redis runs as one replica with the `Recreate` strategy so a
single writer can attach the volume, and the pod is placed on the
`pool-garz-memory` node pool by default.

The existing production and development brokers are ephemeral. Before
enabling this rollout, pause application writes, capture a fresh RDB, transfer
it directly into the pre-created PVC, and verify its checksum and Redis
integrity. The coordinator's migration procedure must complete that cutover
before the old broker is resumed. Keep the PVC and stop for diagnosis if the
new broker does not become healthy.

For a render that intentionally keeps Redis ephemeral, set
`redis.persistence.enabled=false`. That mode omits the PVC, volume, mount,
persistent security context, and `Recreate` strategy. Persistent Redis rejects
any `redis.replicas` value greater than one during Helm rendering.
