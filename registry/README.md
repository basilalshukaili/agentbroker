# Server registry and generated listings

`servers.yaml` supplies the checked-in manifests. Run `python scripts/gen_manifests.py --check` to detect drift, or `--write` to regenerate. `--probe` separately checks the currently advertised endpoints over the network.

AgentBroker retains its published root `server.json`, `glama.json` and `smithery.yaml`. Every other product or capability door writes its enabled listings into `registry/<slug>/`. This applies independently to all three catalogues: adding another product must never replace the first product's files. Product versions are independent; each door follows its own parent product's version.

Publishing flags control artifact generation. `live: false` prevents an unfinished server from appearing in listings or the endpoint probe inventory. Generating a manifest does not deploy a route or submit a listing. A new product still needs its implementation, explicit canonical routing, authorization/billing behavior and verified deployment before its registry entry is made live. Catalogue submission must use that product's generated file.

The current root repository metadata belongs to AgentBroker. Nested generated files provide separate artifacts; they do not prove that a catalogue supports auto-discovering several products from one repository. Verify the target catalogue's supported publication path when adding a product.
