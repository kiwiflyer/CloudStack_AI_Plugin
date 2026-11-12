# ADR-0005: Model artifact storage

**Status:** Proposed — research only, no code changed
**Date:** 2026-11-11
**Deciders:** Simon Weller
**Question asked:** can CloudStack secondary storage replace the dedicated S3
endpoint the engine requires?
**Short answer:** no, but a different CloudStack feature can replace the *credential
and provisioning* half of it, and that is worth doing.

## Context

`aisvc/registry` keeps mirrored model artifacts in an S3-compatible store through
`S3ObjectStore` (boto3), configured by `AISVC_REGISTRY_BUCKET` and
`AISVC_REGISTRY_ENDPOINT` with credentials from boto3's usual chain. The install
guide therefore asks an operator to stand up Ceph RGW, MinIO or S3 and hand the
engine a key.

That is one more thing to provision, and CloudStack already has storage. The
question is whether the engine can use it instead.

## Two different CloudStack features

They are often both called "storage" and they are not the same thing. The answer
turns entirely on telling them apart.

### Secondary storage — not suitable

| | |
| --- | --- |
| What it holds | Templates, ISOs, snapshot backups, uploaded volumes and transfer metadata — CloudStack-tracked resources, each with a database row |
| How it is reached | **Always NFS** |
| Mediated by | The Secondary Storage VM, which owns downloads, cross-zone copies and snapshot backups |
| Object API | None. There is no put/get/list for arbitrary blobs |

Writing model weights into secondary storage would mean putting foreign files into
a filesystem CloudStack believes it owns, with no API to do it through and no
resource row describing them. The admin guide's own warning cuts both ways:
*"If files or folders are not associated with a cloudstack resource, it doesn't
mean that they are not used by cloudstack."* That is a caution against reasoning
about the contents of a store whose layout is CloudStack's business, which is
exactly what this would require us to do.

A GPU node could technically mount the same NFS export and read weights from it.
It would work, and it would be unsupported, undocumented, and dependent on a
layout no contract covers. **Recommendation: no.**

### Object storage — the feature that actually fits

CloudStack gained a pluggable object store framework in 4.19. It manages *pools*
and *buckets*; `plugins/storage/object` in the source tree carries four drivers —
`ceph`, `cloudian`, `minio` and `simulator`.

The decisive detail is what `createBucket` returns. Its response carries
`accesskey`, `usersecretkey` and `url` alongside the bucket's identity. The admin
guide confirms how that is used: the UI's own object browser reads objects with
"the URL, access key, and secret key from the bucket's details".

**So CloudStack does not proxy object data.** It provisions a bucket on a backing
provider and hands back S3 credentials. The design wiki says it outright: CloudStack
manages the lifecycle of pools and buckets, and "Objects will not be managed by
CloudStack."

## What this means for the engine

**It does not remove the S3 dependency. It moves who provisions it.**

An operator still runs Ceph RGW or MinIO. What changes is that CloudStack creates
the bucket on it and issues the keys, instead of an operator doing both by hand and
pasting a credential into `.env`.

`S3ObjectStore` does not change at all. boto3 still talks S3 to the same kind of
endpoint. What changes is where three values come from:

| Today | With object storage |
| --- | --- |
| `AISVC_REGISTRY_ENDPOINT` set by hand | `url` from the bucket |
| `AISVC_REGISTRY_BUCKET` set by hand | `name` from the bucket |
| Credentials from boto3's chain | `accesskey` / `usersecretkey` from the bucket |

The engine already has a signed CloudStack client with `call(cmd, **params)`, so
the work is one call at startup and a fallback to the environment when no object
store pool is configured.

## What it buys, and what it costs

**Buys**

- One fewer credential for an operator to create, distribute and rotate. The engine
  already holds CloudStack keys; it would no longer need a second secret.
- Quota, ownership and accounting visible in CloudStack, where an operator already
  looks. Admins can cap buckets and total object storage per account or domain.
- Consistency with the division of responsibility in CLAUDE.md: CloudStack is the
  IaaS authority, and storage is infrastructure.
- One shorter section in `docs/installation.md`.

**Costs**

- **It is not less infrastructure.** An operator who already runs S3 gains a step:
  they must also register an object storage pool in CloudStack. For them this is
  strictly more setup.
- **Quota becomes a new failure mode.** Quota in GiB is mandatory from 4.21, and
  model artifacts are tens to hundreds of GiB. A mirror that fails at 90% of a
  multi-hour download because a quota was set conservatively is a bad failure, and
  the capacity module cannot see it coming.
- **A bucket is an owned resource.** `createBucket` takes `account`, `domainid` and
  `projectid`. The registry is platform infrastructure, not a tenant's; it would be
  owned by the admin account, and what happens to mirrored artifacts if that bucket
  is deleted through the UI needs an answer before this ships.
- **It raises the floor to 4.19.** [ADR-0003](0003-cloudstack-release.md) targets
  4.23 while holding the plugin to APIs present in 4.22 LTS, so this is inside the
  line — but it makes object storage a hard requirement where today any S3 will do.

## Recommendation

1. **Do not use secondary storage.** It is the wrong shape and the only way in is
   unsupported.
2. **Add CloudStack object storage as a way to obtain the bucket, not as a
   replacement for S3.** `S3ObjectStore` stays. Resolve bucket, endpoint and
   credentials from `listBuckets` / `createBucket` when an object store pool is
   configured, and fall back to `AISVC_REGISTRY_*` when it is not. An operator with
   an existing S3 keeps what they have; one starting fresh gets one less thing to
   build.
3. **Decide the quota and ownership questions before writing code**, because both
   are operational rather than technical: what quota a registry bucket gets, who
   owns it, and what happens when it is deleted out from under the engine.

## The thing this research found that nobody asked about

**Inference pods never read from the registry.** The rendered pod mounts a
node-local `hostPath` weight cache read-only at `/var/lib/aisvc/weights`, with
`HF_HUB_OFFLINE=1` set so the engine cannot fall back to the network. Cloud-init
formats and mounts a node-local NVMe for that cache.

**Nothing populates it.** There is no init container, no sidecar, no DaemonSet and
nothing in cloud-init that copies artifacts from the registry onto a node. The
registry is written by the mirror path and read for manifests; the hop from
registry to node does not exist.

That hop is where the storage decision actually bites — it decides whether a node
pulls over S3, mounts NFS, or is seeded some third way — and it is unbuilt. Choosing
a backend before deciding how a node gets its weights would be settling the easier
question first.

## Sources

- [Storage Overview, CloudStack 4.22 admin guide](https://docs.cloudstack.apache.org/en/4.22.1.0/adminguide/storage.html)
  — buckets, mandatory quota from 4.21, the UI reading objects with the bucket's own
  URL and keys, and the warning about files not associated with a resource.
- [`createBucket`, CloudStack 4.22 API reference](https://cloudstack.apache.org/api/apidocs-4.22/apis/createBucket.html)
  — the parameters, and the `accesskey` / `usersecretkey` / `url` response fields.
- [CloudStack Object Store Framework design wiki](https://cwiki.apache.org/confluence/display/CLOUDSTACK/CloudStack+Object+Store+Framework)
  — the pluggable driver interface, MinIO as the initial provider, and "Objects will
  not be managed by CloudStack". Last updated July 2024; it is a design page, and the
  release API reference is the authority for what shipped.
- [Secondary storage, CloudStack docs](https://docs.cloudstack.apache.org/en/4.19.0.0/adminguide/storage.html)
  — what it holds and that it is reached over NFS.

Provider coverage (`ceph`, `cloudian`, `minio`, `simulator`) was read from directory
listings of `plugins/storage/object` in the CloudStack source tree, which are
undated. **Confirm against the 4.23 tree before relying on Ceph RGW specifically** —
a ShapeBlue presentation from the 4.19 cycle said MinIO was the only supported
provider at the time.
