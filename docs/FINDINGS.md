# Findings

Everything here was measured against a stock **Alfresco Content Services
Community 26.2.0** (schema 21100) running the reference
`docker-compose/community-compose.yaml` from
[`Alfresco/acs-deployment`](https://github.com/Alfresco/acs-deployment).

Measurements are marked **measured**. Anything inferred or unverified says so.
The code comments point here rather than restating the evidence.

---

## 1. Permission enforcement happens in two layers

**measured.** Alfresco does not trust its own search index.

The ACL filter the repository sends to Elasticsearch, captured from the engine's
slow log for a user named `test01`:

```json
"filter": [{"bool": {
    "must_not": [
      {"term": {"DENIED": {"value": "GROUP_EVERYONE"}}},
      {"term": {"DENIED": {"value": "ROLE_AUTHENTICATED"}}},
      {"term": {"DENIED": {"value": "test01"}}}
    ],
    "should": [
      {"term": {"READER": {"value": "GROUP_EVERYONE"}}},
      {"term": {"READER": {"value": "ROLE_AUTHENTICATED"}}},
      {"term": {"READER": {"value": "test01"}}},
      {"term": {"OWNER":  {"value": "test01"}}}
    ]
}}]
```

Then the index was tampered with directly — a user who cannot read a node was
added to that node's `READER` field via a raw `_update`:

```
READER (forged):  ['ROLE_OWNER', 'testuser', 'otrouser']

Elasticsearch, queried alone with otrouser's authorities  →  returns the document
Alfresco Search API as otrouser                           →  hits: 1,  entries: []
```

`hits: 1` with an **empty** `entries` array. The index said yes; the repository
said no. The count comes from Elasticsearch, the entries from the database,
where permissions are re-validated as each node is resolved.

Confirmed in the request Alfresco sends: `"_source": false`. It asks the index
for IDs only, never for content.

### Consequence

Revoking a permission takes effect immediately, before any reindex:

```
permission revoked, then queried at once:
  REST read as the user   →  HTTP 403
  READER in the index     →  still lists the user   (stale)
  Search API as the user  →  document excluded      (correct)
```

**Alfresco fails closed. A vector store with a copied ACL fails open.**

---

## 2. Copying the ACL leaks

**measured.** A document's permissions were changed in Alfresco (inheritance
cut, restricted to two authorities) while a separate index held a copy of its
`READER`:

```
22 seconds after the change:
  READER in Alfresco's index  →  ['GROUP_site_swsdp_SiteManager', 'admin']
  READER in the copy          →  ['GROUP_EVERYONE', + 4 site groups]

A user with no access queried the copy  →  received all 3 chunks of the
                                            restricted document.
```

No error, no warning. Silent disclosure for as long as the copy stays stale.

### And the authority list is easy to get wrong

Two traps, both measured.

**`ROLE_OWNER` is a dynamic authority, not a group.** A document whose `READER`
is `["ROLE_OWNER"]` with `OWNER: "testuser"`:

```
naive  terms filter on READER        →  testuser does NOT see their own document
correct filter (+ OWNER == user)     →  testuser sees it
```

There were 15 such documents in a freshly installed repository.

**`ROLE_AUTHENTICATED` belongs in the list** and is easy to forget. Alfresco
uses three authorities plus `OWNER`; a hand-built filter that only uses the
username and `GROUP_EVERYONE` hides every document granted to
`ROLE_AUTHENTICATED`.

### Alfresco's own proof of concept chose to copy, and it cost them

[`AlfrescoLabs/content-lake-app`](https://github.com/AlfrescoLabs/content-lake-app)
stores the ACL in its index. Its documentation states the problem plainly:

> Alfresco Repository does **not reliably emit permission update events** for
> the ACL changes needed by Content Lake.

So they built a **Java addon deployed inside the repository** to detect ACL
changes post-commit, a persistent ActiveMQ queue, a transacted listener, a
`POST /api/sync/permissions` endpoint with recursive descendant traversal, and
a `modifiedAt` staleness check — and still document a case that stays stale.

Their security model also warns:

> **No enforcement at all if a caller reaches the index directly.** The
> predicate is applied by the client. Bypassing the client bypasses the
> predicate. **This is the single most important property to understand about
> this design.**

---

## 3. Event2 emits nothing for permission changes

**measured, three times.** Counting messages published to the
`alfresco.repo.event2` topic per operation:

| Operation | Events |
|---|---|
| Create a document | 3 |
| Replace the content | 1 |
| Rename | 2 |
| Change a property | 1 |
| Move between folders | 1 (+1 per affected container) |
| Move to a site | 1 (+1 per affected container) |
| Delete | 2 |
| **Change permissions** | **0** |
| **Start a workflow** | **0** |
| **Complete a task** | **0** |

The type exists in the model. From `acs-event-model-1.1.0-A.1.jar`,
`EventType`:

```
NODE_CREATED  NODE_UPDATED  NODE_DELETED
CHILD_ASSOC_CREATED  CHILD_ASSOC_DELETED
PEER_ASSOC_CREATED   PEER_ASSOC_DELETED
AUDIT_ENTRY_CREATED
PERMISSION_UPDATED          ← defined, never emitted
```

No property enables it: all 20 `repo.event2.*` properties in
`repository.properties` were checked.

**This is the strongest argument for the design in this project.** The half of
the change stream you cannot observe is exactly the half you do not need to
store.

---

## 4. The payload field is `resourceBefore`

**measured.** Not `resourceBeforeUpdate`. Getting this wrong yields an empty
changed-field set on every event and silently reduces the classifier to
"re-embed everything".

It holds **only the attributes that changed**:

| Operation | `resourceBefore` keys |
|---|---|
| Replace the content | `content`, `modifiedAt`, `properties` |
| Move | `primaryHierarchy`, `properties` |
| Rename | `name`, `primaryHierarchy`, `properties` |
| Change `cm:title` | `localizedProperties`, `modifiedAt`, `properties` |
| Create / Delete | `[]` |

### Move versus rename

Both change `primaryHierarchy`, because the primary association's qualified
name embeds the node name. `name` is what separates them.

A move carries **both** hierarchies, so the old and new parent are in the same
message:

```
before : [ce85adf5(source_folder), 7f9f8cf0, 6f6dab3f, 51360a3f]
after  : [53de17dc(target_folder), 7f9f8cf0, 6f6dab3f, 51360a3f]
```

First element is the immediate parent. There is **no readable path** in the
event — only node IDs — so resolving a site needs one metadata call:

```
GET /nodes/{id}?include=path
  path.name: /Company Home/Sites/swsdp/documentLibrary/Agency Files/Contracts
  elements:  ['Company Home','Sites','swsdp','documentLibrary','Agency Files','Contracts']
             → the element after 'Sites' is the site: swsdp
```

Nodes outside a site return no `Sites` element, so the site is `None`.

### Two sources of noise

**A spurious `Updated` right after every create**, carrying only `aspectNames`
— Alfresco applying `cm:versionable`. Acting on it embeds the document twice.

**Containers get an `Updated` carrying only `modifiedAt`** whenever a child is
added, removed or moved.

---

## 5. Workflows are visible only through their documents

**measured.** Starting a process and completing a task produced zero events. A
workflow with a document attached produced two:

```
Updated  cm:content  the-document  resourceBefore=['properties','secondaryParents']
Created  (association)
```

The second one:

```json
{"type": "org.alfresco.event.assoc.child.Created",
 "data": {"resource": {"@type": "ChildAssociationResource",
                       "assocType": "bpm:packageContains",
                       "parent": {"id": "..."},   ← the bpm:package node
                       "child":  {"id": "..."}}}}  ← the document
```

`bpm:packageContains` is the signal. For task state or process completion there
is no event — the workflow REST API accepts date filters
(`where=(startedAt > '...')`), so a watermark poll is the only option.

---

## 6. Text extraction through the transform service works and is better

**measured.** A 6-page, 381,778-byte PDF:

```
POST /transform  file=@doc.pdf  sourceMimetype=application/pdf  targetMimetype=text/plain
  → HTTP 200, 12,494 bytes of text
```

Using the repository's own extractor rather than a local library means the text
you embed is the text Alfresco indexed, and every format the repository
supports works with nothing per-format to maintain.

### Health-check `/ready`, not `/actuator/health`

**measured.** On a healthy transform service:

```
/actuator/health            →  {"status":"DOWN"}  HTTP 503
/actuator/health/liveness   →  {"status":"UP"}    HTTP 200
/actuator/health/readiness  →  {"status":"UP"}    HTTP 200
/ready                      →  "Success - No transform."  HTTP 200
POST /transform             →  HTTP 200 (actually converts)
```

The aggregate endpoint is a false negative. `/ready` is what the official
compose healthcheck uses.

---

## 7. Metrics: the repository has none, two components do

**measured.**

```
alfresco:8080/alfresco/s/prometheus     →  HTTP 404   (and no metrics.* properties)
batch-indexing:8080/actuator/prometheus →  HTTP 200   (1,595 metric lines)
transform:8090/actuator/prometheus      →  HTTP 200
```

The repository runs on Tomcat and is not a Spring Boot application, so it has
no Actuator. The other two are, and get it for free.

The metric worth alerting on:

```
alfresco_reindex_continuous_last_activity_timestamp
alfresco_reindex_continuous_last_cycle_finished_timestamp
```

```promql
(time() * 1000 - alfresco_reindex_continuous_last_activity_timestamp) / 1000 > 300
```

If indexing stops, nothing looks broken — documents upload, display and
download — but search freezes in time. The only other health signals the
repository offers are its probes:

```
/probes/-ready-   →  200   can serve requests
/probes/-live-    →  200   the process is alive
/probes/-health   →  400   "Bad probe name" — this one does not exist
```

`batch-indexing` has Actuator but **no JVM binder**: it exposes `alfresco_`,
`spring_`, `executor_`, `http_` and `tomcat_` metrics, and zero `jvm_*`.

Its `alfresco_*` series are labelled per `jobId`, and a new job id is created
every 15-second cycle (rolling window of 50). Drop the label when scraping or
Prometheus accumulates thousands of series a day.

---

## 8. Scaling limits, in the order they bite

**measured** on a stock install.

**Tomcat threads: 200.** The shipped `server.xml` declares no `maxThreads`, so
Tomcat's default applies. A content download holds a thread for the whole
transfer, so concurrent downloads of large documents are the first ceiling.

**PostgreSQL connections: five spare.** `db.pool.max=275`, `batch-indexing`
holds ~20 (measured via `pg_stat_activity`), and `max_connections=300`. Nothing
else fits without raising the limit, and exhausting the pool stops the
repository from responding. This is why a RAG vector store belongs on its own
PostgreSQL instance.

**Node caches are sized for 250,000.** `cache.node.nodesSharedCache.maxItems`
is 250,000; the aspects, properties, parentAssocs, contentData and contentUrl
caches are 130,000 each. Past that, repositories thrash and the database takes
the load. What matters is the working set, not the total document count.

**Search is cheap; returning results is not.** Because of the two layers, a
thousand results is a thousand node loads from PostgreSQL with permission
re-validation. Paginate.

**Not a limit:** files per directory. The content store buckets by date
(`contentstore/YYYY/MM/DD/HH/mm/<uuid>.bin`) precisely so no directory fills
up. The real volume limits are filesystem inodes, database size and the
memory the Lucene index wants.

### The ACL filter grows linearly but did not measurably cost anything

**measured, with a caveat.** A user in 61 groups:

```
READER terms   : 63
DENIED terms   : 63
OWNER terms    :  1
total clauses  : 127
query size     : 6,297 characters
```

Timing the same query for that user against a 3-authority user, 8 runs each:

```
3 authorities   →  avg  75 ms
63 authorities  →  avg  78 ms
```

**No measurable difference.** The caveat is real though: this index held 862
documents. At that size 127 inverted-index term lookups cost nothing. Whether
it holds at millions of documents was not tested.

---

## 9. Content is stored unencrypted

**measured.** The content store holds the original bytes, renamed:

```
alf_data/contentstore/2026/11/03/14/44/1a592c40-2b6f-4cd3-992c-402b6f5cd3b8.bin
```

274 files, all `.bin`, and their magic bytes are the originals:

```
\211 P N G \r \n \032 \n                → PNG
\377 \330 \377 \341                     → JPEG
\320 \317 \021 \340 \241 \261 \032 \341 → OLE2 (legacy Office)
< h t m l >                             → plain HTML
```

EXIF metadata reads straight out: `Canon EOS 300D DIGITAL`,
`Adobe Photoshop CS5 Macintosh`.

**This matters for non-production environments.** Anonymising the database does
nothing to the files: a clinical record PDF copied to a development environment
*is* a clinical record. Anonymisation has to cover both the metadata and the
content store, and the second is the hard part.

Alfresco supports encrypting the content store; it is explicit configuration,
not the default.

---

## 10. Audit is reported as enabled but records nothing

**measured.**

```properties
audit.enabled=true                      ← what the discovery API reports
audit.alfresco-access.enabled=false     ← the application that records access
audit.cmischangelog.enabled=false
audit.dod5015.enabled=false
audit.tagging.enabled=true
```

With defaults, `GET /audit-applications` returns **only** `tagging`. A stock
install does **not** record who read or modified what, despite
`isAuditEnabled: true` in the discovery response.

Enabling `alfresco-access` and checking what it captures:

```
/alfresco-access/transaction/action       = CREATE
/alfresco-access/transaction/user         = admin
/alfresco-access/transaction/path         = /app:company_home/app:shared/cm:audited.txt
/alfresco-access/transaction/type         = cm:content
/alfresco-access/transaction/sub-actions  = createNode updateNodeProperties createContent
                                             addNodeAspect createVersion
```

It can be scoped by path, user and type via `audit.filter.alfresco-access.*`
(`~` negates, `;` separates), and the REST API is complete — all four endpoints
returned 200, including `GET /nodes/{id}/audit-entries` for a single document's
history. Purging works too (`DELETE ...?where=(createdAt BETWEEN (...))` → 204),
but **there is no automatic purge**: the tables grow indefinitely.

Relevant if you need an access trail for what a retrieval pipeline surfaced.

---

## 11. Not available in Community Edition

**measured** — each verified by calling the endpoint or inspecting the image.

| Capability | Status |
|---|---|
| Repository clustering | no `alfresco.cluster.*` properties, no subsystem |
| S3 / Azure / encrypted content store | `ContentStore` subsystem offers one option: `unencrypted` |
| SAML authentication | no SAML jar present |
| Security Controls (marks, clearances) | `/gs-security/.../security-groups` → HTTP 400 |
| Digital Workspace (ADW) | `quay.io/alfresco/alfresco-digital-workspace` → 401 |
| v1 folder rules API | `/nodes/{id}/rule-sets` → 404 from `ResourceLookupDictionary`, while sibling relations return 200. The classes and Spring beans are present in the jar. The legacy webscript `/alfresco/s/api/node/{ref}/ruleset/rules` works. Whether this is Enterprise-gated or a registration defect was **not determined** — it would need an Enterprise instance to tell apart. |

Available in Community, contrary to a common assumption: **Governance Services**
(`alfresco-governance-repository-community`, actively released). Process
Services (APS) is Enterprise only, but its engine — Activiti — is open source
and is embedded in ACS.

---

## Reproducing any of this

The `docker-compose.yml` in this repository brings up the same stack. Most of
the measurements above are a `curl` away; the index-level ones need
Elasticsearch's slow log, which is off by default:

```bash
curl -X PUT localhost:9200/alfresco/_settings \
  -H 'Content-Type: application/json' \
  -d '{"index.search.slowlog.threshold.query.warn":"0ms"}'

docker compose logs elasticsearch | grep slowlog
```

The slow log writes to the container's stdout, not to a file — the Alfresco
Elasticsearch image routes its appenders to the console.

Corrections welcome: open an issue with the version you measured on.
