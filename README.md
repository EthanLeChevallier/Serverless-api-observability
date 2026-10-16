# Serverless API Observability — démo locale

Surveille des APIs publiques avec Python, `httpx` asynchrone et OpenTelemetry, puis visualise métriques, logs et traces avec une stack locale Docker Compose :

- Prometheus pour les métriques ;
- Loki pour les logs ;
- Tempo pour les traces ;
- Grafana pour les dashboards et l'alerte p95.

## Coûts et périmètre

Cette version ne déploie **aucune ressource AWS** et n'envoie pas de télémétrie vers Grafana Cloud. Les composants tournent sur ta machine. Docker Desktop peut avoir ses propres conditions de licence ; le dépôt ne peut pas garantir que l'ensemble de l'environnement informatique est sans coût.

Le workflow GitHub Actions exécute uniquement Ruff et Pytest. Il ne possède ni credentials cloud ni étape de déploiement. Les minutes GitHub Actions restent soumises aux quotas du plan du compte.

Le bucket AWS `serverless-api-observability-tfstate-prod` que tu as créé n'est pas utilisé par cette version. Si tu ne veux plus conserver de ressource AWS, vérifie qu'il ne contient aucun objet/version utile, puis supprime-le depuis S3. Ne supprime pas d'autres ressources AWS sans les avoir inspectées.

## Prérequis

- Docker Desktop installé et démarré, avec Docker Compose v2 (`docker compose`)
- Python 3.11+ si tu veux lancer les tests hors conteneur
- Accès internet pour télécharger les images au premier démarrage et interroger les APIs configurées

## Démarrage

À la racine du dépôt :

```bash
cp .env.example .env
```

Ouvre `.env` et change `GRAFANA_ADMIN_PASSWORD` par un mot de passe local. Le fichier `.env` est ignoré par Git.

Démarre la stack :

```bash
docker compose up --build -d
```

Le premier lancement télécharge les images et peut prendre quelques minutes. Vérifie les conteneurs :

```bash
docker compose ps
docker compose logs -f api-monitor
```

Ouvre [http://localhost:3000](http://localhost:3000) et connecte-toi avec l'utilisateur et le mot de passe indiqués dans `.env`. Le dashboard **Serverless API Observability (Local)** est provisionné automatiquement. La première vérification démarre immédiatement, puis le runner vérifie les APIs toutes les cinq minutes.

Pour arrêter les conteneurs sans supprimer les données :

```bash
docker compose down
```

Pour supprimer aussi les données locales de Grafana, Prometheus, Loki et Tempo :

```bash
docker compose down -v
```

Cette dernière commande est destructive pour les données d'observabilité locales.

## Configuration

Les valeurs sont lues depuis `.env` par Docker Compose :

| Variable | Défaut | Description |
|---|---|---|
| `API_TARGETS` | GitHub API et httpbin | URLs HTTP(S) séparées par des virgules |
| `API_CONCURRENCY` | `10` | Maximum de requêtes simultanées |
| `HTTP_TIMEOUT_SECONDS` | `10` | Timeout par requête |
| `CHECK_INTERVAL_SECONDS` | `300` | Intervalle entre deux cycles |
| `GRAFANA_ADMIN_USER` | `admin` | Identifiant Grafana local |
| `GRAFANA_ADMIN_PASSWORD` | `change-this-local-password` | Mot de passe Grafana local ; à remplacer |

Les endpoints OTLP internes sont configurés dans `compose.yaml`. Aucun token Grafana Cloud ni identifiant AWS n'est requis.

## Signaux et alerte

Le runner réutilise la fonction de vérification asynchrone du handler, dans un processus local continu. Chaque cible produit :

- un compteur `api_check_requests_total` ;
- un histogramme `api_check_duration_seconds` avec des buckets explicites de 50 ms à 10 s et des bornes rapprochées autour du seuil d'alerte de 500 ms ;
- un span OpenTelemetry `api.health_check` ;
- un log structuré avec statut HTTP et latence.

Le Collector reçoit OTLP/HTTP puis exporte les métriques vers son endpoint Prometheus, les logs vers Loki et les traces vers Tempo. Grafana préconfigure ces trois sources et charge un dashboard depuis `observability/grafana/`.

Le dashboard présente les quantiles p50/p95/p99, le débit des checks, le pourcentage de checks sains sur 15 minutes, les codes HTTP et la latence moyenne, en plus des logs Loki et des traces Tempo.

Une règle provisionnée évalue le p95 par API à partir d'une fenêtre Prometheus de cinq minutes et passe en alerte si la latence dépasse 0,5 seconde pendant cinq minutes. Les percentiles sont interpolés à partir de buckets et restent des estimations, particulièrement avec un contrôle seulement toutes les cinq minutes : attends plusieurs cycles et interprète les tendances avec prudence. Pour un SLI d'uptime statistiquement robuste, il faut une fréquence d'échantillonnage plus élevée. La fréquence est réglable avec `CHECK_INTERVAL_SECONDS`.

Les métriques utilisent des fenêtres glissantes de 5 ou 15 minutes. Au démarrage, l'absence de points ou une fenêtre trop courte peut laisser certains panneaux vides ; attends plusieurs cycles. Les requêtes PromQL sont définies dans `observability/grafana/dashboards/serverless-api-observability.json`. Si un panneau reste vide, vérifie le nom réel de la série dans Grafana Explore (source Prometheus) et les labels `api_target`, `result` et `status_code`.

## Développement et tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check src tests
python -m pytest -q
```

Le workflow `.github/workflows/ci.yml` relance Ruff et Pytest sur les pull requests et les pushes vers `main`. Il ne déploie rien dans AWS ou Grafana Cloud.

## Arborescence utile

```text
.github/workflows/ci.yml
src/config.py
src/main.py
src/runner.py
tests/test_main.py
compose.yaml
observability/
  otel-collector.yaml
  prometheus.yaml
  loki.yaml
  tempo.yaml
  grafana/
    dashboards/
    provisioning/
Dockerfile
requirements.txt
requirements-dev.txt
.env.example
```
