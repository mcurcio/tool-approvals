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
