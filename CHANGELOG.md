## [0.6.0](https://github.com/mcurcio/tool-approvals/compare/v0.5.0...v0.6.0) (2026-11-24)

### Features

* **chart:** pin the image by digest and take the version label from appVersion ([e2f59df](https://github.com/mcurcio/tool-approvals/commit/e2f59df2be011ea8457213915574e66ca488656a))
* **chart:** refuse a malformed image repository or digest, and name the image in the notes ([706d312](https://github.com/mcurcio/tool-approvals/commit/706d312ed39a2d56b71264fec6bda00d68805768))

## [0.5.0](https://github.com/mcurcio/tool-approvals/compare/v0.4.0...v0.5.0) (2026-11-20)

### Features

* **approvals:** add the milestone 3 interfaces ([d59821b](https://github.com/mcurcio/tool-approvals/commit/d59821b73120ecbc47c669b8f6b8ef9c5d7ef1a4))
* **approvals:** deliver approval events to channels and serve the audit report ([a9c2519](https://github.com/mcurcio/tool-approvals/commit/a9c2519b08304f82b648e382feac7f03e670167b))
* **approvals:** serve the Answers API v1 for integrated surfaces ([4f89bbb](https://github.com/mcurcio/tool-approvals/commit/4f89bbb13ecd691575deed9f48ac8b72b5a41c61))
* **chart:** add the Helm chart and compose file, released with the image ([0c0cc5c](https://github.com/mcurcio/tool-approvals/commit/0c0cc5cf894d5345d1edc819c40820e77e7990bd))
* **desk:** list the Approvals waiting for the signed-in approver and register browsers for push ([f6952b8](https://github.com/mcurcio/tool-approvals/commit/f6952b86b2a4a3a040e202cd08b169ea29005b2a))

### Bug Fixes

* **approvals:** write the desk surface as the via of push subscription changes ([1b059c2](https://github.com/mcurcio/tool-approvals/commit/1b059c29a33eaf40678a641560924589a5504fa9))
* **chart:** render the NetworkPolicies by default ([3a8cbd1](https://github.com/mcurcio/tool-approvals/commit/3a8cbd174bf699b09ebcee47e80f9c6ac6ae410d))
* **compose:** publish the ports on loopback unless TOOL_APPROVALS_BIND says otherwise ([ad2a657](https://github.com/mcurcio/tool-approvals/commit/ad2a65759597f9d00967e571d23befd9cf226ec0))
* **delivery:** count the give-up horizon from the first failure, and give each push subscription its own outcome ([05b0256](https://github.com/mcurcio/tool-approvals/commit/05b0256c27301c29bbf0b75a8a896e1bd62d3c19))
* **desk:** allow first-party script only on the push page ([900206a](https://github.com/mcurcio/tool-approvals/commit/900206abaa22a5173c23981b3c0baeccac362f4f))
* **desk:** scope the push worker, renew on visit, keep a stepped-up sign-in ([9559a35](https://github.com/mcurcio/tool-approvals/commit/9559a35780dc44dde6dde4bf67b485b98846c6cd))
* **push:** stop routing a subscription past its max age ([84c2dea](https://github.com/mcurcio/tool-approvals/commit/84c2dea0cc488b0b454bca47383963ddbc1754a1))
* **store:** bind routed_to's groups through the column's GroupsJson type ([d26528e](https://github.com/mcurcio/tool-approvals/commit/d26528e53c8875263088c99094c39f4f5d478b1d))

## [0.4.0](https://github.com/mcurcio/tool-approvals/compare/v0.3.0...v0.4.0) (2026-11-19)

### ⚠ BREAKING CHANGES

* **wait:** AwaitOutcomeRequest gains principal, and Approvals answers
a read without it as an unknown wait. agentgateway deployments must add the
wait target's request transformation, and Check API gateways must set the
principal header on every message to the wait route, or agents cannot read
their own Wait Tasks.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

### Features

* **gate:** one message rule for every gateway; tasks/* pass behind Envoy ([2b703f7](https://github.com/mcurcio/tool-approvals/commit/2b703f7445bc1fee87992c74811b6a4fae461edb))
* **gate:** one wait-route rule for every adapter; Envoy passes every MCP method but tools/call ([86ccd14](https://github.com/mcurcio/tool-approvals/commit/86ccd14a5e3a4b923066717d036974fa478558b2))

### Bug Fixes

* **gate:** answer every refused notification with HTTP 400 and no id ([792b2c2](https://github.com/mcurcio/tool-approvals/commit/792b2c257886bb1241cce5d77ed4a44b6c244b46))
* **gate:** refuse a method that only reads as tools/call ([7416c31](https://github.com/mcurcio/tool-approvals/commit/7416c313f781b70e75c3c59e15ae415c9cd3de9a))
* **gate:** refuse an Envoy notification with HTTP 400 and no id ([3d9a205](https://github.com/mcurcio/tool-approvals/commit/3d9a205ae450459cc842a8c8898ed129bdc717d1))
* **wait:** bind every wait to the principal that opened it ([1369219](https://github.com/mcurcio/tool-approvals/commit/1369219ff6e0f09f1ae5e3ccc4f3240f4cb63e5f))
* **wait:** fail closed mid-upgrade and answer another's ticket as unknown ([304e5b8](https://github.com/mcurcio/tool-approvals/commit/304e5b8de4198befb456ddf993a3b5358672c308))
* **wait:** read the principal header by the gate's own principal rule ([81b3ec8](https://github.com/mcurcio/tool-approvals/commit/81b3ec81997d6cbb1136c04a16343b1725ae7f4c))

## [0.3.0](https://github.com/mcurcio/tool-approvals/compare/v0.2.1...v0.3.0) (2026-11-18)

### Features

* **approvals:** re-check the answer's moment at claim ([7a80133](https://github.com/mcurcio/tool-approvals/commit/7a8013352e67ed01eb2d5205f2800dc4d8735b70))
* **auth:** accept mTLS as a registry credential beside the bearer ([c502548](https://github.com/mcurcio/tool-approvals/commit/c50254839a7e66deea6b9754142988bd7c2c2a62))
* **gate:** classify ext_authz requests as a pure function; one conformance suite for every adapter ([d30e367](https://github.com/mcurcio/tool-approvals/commit/d30e367aeef1957267f71d0a4a8eff6aa7f1c806))
* **gate:** declare the gateway port (GatewayContext, Checker) and import contract C9 ([98a65e5](https://github.com/mcurcio/tool-approvals/commit/98a65e5be1682b50d09a376e537ad3a2d29aa15e))
* **gate:** every adapter maps into GatewayContext; gateway-context policy and inbound deadlines ([a2d6964](https://github.com/mcurcio/tool-approvals/commit/a2d696437a6311aada88175264489ae29b8dd075))

### Bug Fixes

* **approvals:** enforce R1's sign-in rule at claim from one kernel predicate ([2f33064](https://github.com/mcurcio/tool-approvals/commit/2f3306461b843083c0260513f8a8424d56b3a5b3))
* **e2e:** publish every port the harness dials from one list ([839287c](https://github.com/mcurcio/tool-approvals/commit/839287c95cde10e916d9638ef9276b849cb1c51f))
* **gate:** make Envoy's types an optional envoy extra, loaded only for gate.ext_authz ([6ab3219](https://github.com/mcurcio/tool-approvals/commit/6ab32195d0a881a9cf8604a6f3f18f6f59c2020a))
* **serving:** serve HTTP only, so no WebSocket scope misses the TLS extension ([b140143](https://github.com/mcurcio/tool-approvals/commit/b140143c85831fe3a4f1b4e9ec467f84fc429661))
