{{/* The chart's name, and the release-scoped name every object starts with. */}}
{{- define "tool-approvals.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tool-approvals.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "tool-approvals.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "tool-approvals.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "tool-approvals.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels of one component: (dict "root" $ "component" "gate"). */}}
{{- define "tool-approvals.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tool-approvals.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
The app.kubernetes.io/version label: the chart's appVersion (the release's version), never image.tag, so no tag or
digest can make the label invalid. Made a valid label value: characters outside [-A-Za-z0-9_.] become "_" (semver's
"+"), at most 63 characters, starting and ending alphanumeric.
*/}}
{{- define "tool-approvals.version" -}}
{{- $version := regexReplaceAll "[^-A-Za-z0-9_.]" (toString .Chart.AppVersion) "_" }}
{{- $version = regexReplaceAll "^[^A-Za-z0-9]+" $version "" | trunc 63 }}
{{- regexReplaceAll "[^A-Za-z0-9]+$" $version "" }}
{{- end }}

{{- define "tool-approvals.tag" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{/*
Every container's image. With image.digest, repository@digest: the digest alone picks the image (a tag beside it is
ignored by the runtime), so image.tag is not used. Without it, repository:tag.
*/}}
{{- define "tool-approvals.image" -}}
{{- with .Values.image.digest }}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .) }}
{{- fail (printf "image.digest must be sha256: followed by 64 lowercase hex digits, got %q" .) }}
{{- end }}
{{- printf "%s@%s" $.Values.image.repository . }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (include "tool-approvals.tag" .) }}
{{- end }}
{{- end }}

{{- define "tool-approvals.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "tool-approvals.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* The configuration file, which the chart cannot render without. */}}
{{- define "tool-approvals.config" -}}
{{- if not .Values.config }}
{{- fail "config is required: set `config` to the tool-approvals configuration file as a YAML object (docs/reference/configuration.md)" }}
{{- end }}
{{- toYaml .Values.config }}
{{- end }}

{{/*
Each listener a service's configuration names, as YAML `items: [{name, port, tls}]`, health first. The names are
the Service port names; the keys are where the configuration file holds each listener. A listener the file leaves
out is not rendered. (dict "root" $ "service" "gate")
*/}}
{{- define "tool-approvals.listeners" -}}
{{- $_ := include "tool-approvals.config" .root }}
{{- $config := .root.Values.config }}
{{- $paths := dict
  "gate" (list
    (list "health" "gate" "listeners" "health")
    (list "check-api-grpc" "gate" "check_api" "grpc")
    (list "check-api-json" "gate" "check_api" "json")
    (list "ext-mcp" "gate" "ext_mcp" "listener")
    (list "ext-authz" "gate" "ext_authz" "listener"))
  "wait" (list
    (list "health" "wait" "listeners" "health")
    (list "mcp" "wait" "listeners" "mcp"))
  "approvals" (list
    (list "health" "approvals" "listeners" "health")
    (list "requests" "approvals" "listeners" "requests")
    (list "approvers" "approvals" "listeners" "approvers")
    (list "answers-grpc" "approvals" "listeners" "answers_grpc")
    (list "answers-json" "approvals" "listeners" "answers_json")
    (list "admin" "approvals" "listeners" "admin")) }}
{{- if not (get $config .service) }}
{{- fail (printf "config.%s is required while %s.enabled is true" .service .service) }}
{{- end }}
{{- $items := list }}
{{- range (get $paths .service) }}
{{- $name := first . }}
{{- $keys := rest . }}
{{- $listener := dig (index $keys 0) (index $keys 1) (index $keys 2) dict $config }}
{{- if $listener }}
{{- if not $listener.port }}
{{- fail (printf "config.%s has no port" (join "." $keys)) }}
{{- end }}
{{- $items = append $items (dict "name" $name "port" (int $listener.port) "tls" (not (empty $listener.tls))) }}
{{- else if eq $name "health" }}
{{- fail (printf "config.%s is required: the chart probes it" (join "." $keys)) }}
{{- end }}
{{- end }}
{{- toYaml (dict "items" $items) }}
{{- end }}

{{/*
The container's volumes and mounts: the configuration file, and the named Secrets read-only.
(dict "configMap" NAME "secret" NAME "tls" NAME)
*/}}
{{- define "tool-approvals.volumeMounts" -}}
- name: config
  mountPath: /etc/tool-approvals/config.yaml
  subPath: config.yaml
  readOnly: true
{{- if .secret }}
- name: secrets
  mountPath: /etc/tool-approvals/secrets
  readOnly: true
{{- end }}
{{- if .tls }}
- name: tls
  mountPath: /etc/tool-approvals/tls
  readOnly: true
{{- end }}
{{- end }}

{{- define "tool-approvals.volumes" -}}
- name: config
  configMap:
    name: {{ .configMap }}
{{- if .secret }}
- name: secrets
  secret:
    secretName: {{ .secret }}
{{- end }}
{{- if .tls }}
- name: tls
  secret:
    secretName: {{ .tls }}
{{- end }}
{{- end }}

{{/* The command's arguments: the configuration file, then the command. */}}
{{- define "tool-approvals.args" -}}
{{- $commands := dict "gate" (list "gate") "wait" (list "wait") "approvals" (list "approvals" "serve") "migrate" (list "approvals" "migrate") }}
{{- toJson (concat (list "--config" "/etc/tool-approvals/config.yaml") (get $commands .)) }}
{{- end }}
