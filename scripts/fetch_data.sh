#!/usr/bin/env bash
# Refresh data/contributions.json from the GitHub GraphQL API.
#
#   scripts/fetch_data.sh [output.json]        needs: gh (authenticated), jq, python3
#
# Env: PROFILE_LOGIN (default: lonerry). In Actions, GH_TOKEN is the workflow token.
# Public contributions only, unless GH_TOKEN is a PAT of the profile owner and
# "Include private contributions on my profile" is on in GitHub settings.
#
# Output schema (read by scripts/render_cards.py):
#   { login, from, to, total,
#     days:  [ {date: "YYYY-MM-DD", count: n}, ... ],          # oldest first
#     profile: { public_repos: n },
#     repos: [ {name, description, url, stars, forks, pushedAt, language, languageColor}, ... ] }
set -euo pipefail

LOGIN="${PROFILE_LOGIN:-lonerry}"
OUT="${1:-data/contributions.json}"

# The API accepts at most one year per query: last 364 days, plus today.
FROM="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)-d.timedelta(days=364)).strftime("%Y-%m-%dT00:00:00Z"))')"
TO="$(python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')"

QUERY='
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    repositories(privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false, first: 100,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes { name description url stargazerCount forkCount pushedAt
              primaryLanguage { name color } }
    }
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}'

JQ='
.data.user as $u
| [$u.contributionsCollection.contributionCalendar.weeks[].contributionDays[]] as $d
| {
    login: $login,
    from: $d[0].date,
    to: $d[-1].date,
    total: $u.contributionsCollection.contributionCalendar.totalContributions,
    days: [$d[] | {date, count: .contributionCount}],
    profile: {public_repos: $u.repositories.totalCount},
    repos: [$u.repositories.nodes[] | {
      name, description, url,
      stars: .stargazerCount, forks: .forkCount, pushedAt,
      language: (.primaryLanguage.name // null),
      languageColor: (.primaryLanguage.color // null)
    }]
  }'

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

gh api graphql -f query="$QUERY" -f login="$LOGIN" -f from="$FROM" -f to="$TO" \
  | jq --arg login "$LOGIN" "$JQ" > "$TMP"

# never replace good data with an empty/broken response
jq -e '(.days | length) > 0' "$TMP" > /dev/null
mv "$TMP" "$OUT"
trap - EXIT
echo "wrote $OUT: $(jq -r '"\(.days | length) days, \(.total) contributions, \(.repos | length) repos"' "$OUT")"
