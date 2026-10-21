#!/usr/bin/env bash
# One command: package the notice writer, build and deploy the stack, publish the dashboard.
#
#   scripts/deploy.sh
#
# Identity parameters (RCS_AGENT_ID, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_TEMPLATE_NAME, SES_SENDER) are
# optional. A variable you do not set keeps the value the deployed stack already has, so adding the
# WhatsApp id later never blanks the RCS id. Set a variable to "-" to clear it on purpose.
# WINDOW_SECONDS (default 90) is the length of one escalation window.
set -euo pipefail

for tool in aws sam python3 zip shasum; do
  command -v "$tool" >/dev/null || { echo "missing prerequisite: $tool" >&2; exit 1; }
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
STACK="${STACK:-proof-of-reach}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
CODE_BUCKET="por-agent-code-${ACCOUNT}-${REGION}"

# Value for a parameter: the environment wins, else what the stack has now, else empty.
param() {
  local name="$1" env_value="$2" current
  if [ "$env_value" = "-" ]; then echo ""; return; fi
  if [ -n "$env_value" ]; then echo "$env_value"; return; fi
  current="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Parameters[?ParameterKey=='$name'].ParameterValue" --output text 2>/dev/null || true)"
  [ "$current" = "None" ] && current=""
  echo "$current"
}
RCS_VALUE="$(param RcsAgentId "${RCS_AGENT_ID:-}")"
WA_VALUE="$(param WhatsAppPhoneNumberId "${WHATSAPP_PHONE_NUMBER_ID:-}")"
TEMPLATE_VALUE="$(param WhatsAppTemplateName "${WHATSAPP_TEMPLATE_NAME:-}")"
SES_VALUE="$(param SesSender "${SES_SENDER:-}")"
echo "==> identities: RcsAgentId='${RCS_VALUE}' WhatsAppPhoneNumberId='${WA_VALUE}' WhatsAppTemplateName='${TEMPLATE_VALUE}' SesSender='${SES_VALUE}'"

echo "==> packaging the notice writer"
BUILD="$ROOT/.build/agent"
rm -rf "$BUILD" && mkdir -p "$BUILD"
# boto3 is pure Python, so the zip needs no arm64 wheels. Pin matches src/requirements.txt.
python3 -m pip install --quiet --target "$BUILD" -r src/requirements.txt
cp agent_runtime/main.py src/agent/notice_text.py "$BUILD/"
find "$BUILD" -name __pycache__ -type d -prune -exec rm -rf {} +
chmod -R u+rwX,go+rX "$BUILD"
ZIP="$ROOT/.build/agent.zip"
rm -f "$ZIP" && (cd "$BUILD" && zip -qrX "$ZIP" .)
KEY="agent/$(shasum -a 256 "$ZIP" | cut -c1-16).zip"   # new content gets a new key, so the runtime updates

echo "==> uploading to s3://$CODE_BUCKET/$KEY"
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" --region "$REGION" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$CODE_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$CODE_BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
fi
aws s3 cp "$ZIP" "s3://$CODE_BUCKET/$KEY" --only-show-errors

echo "==> sam build"
sam build >/dev/null

echo "==> sam deploy ($STACK)"
sam deploy --stack-name "$STACK" --region "$REGION" --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides \
    "ParameterKey=AgentCodeBucket,ParameterValue=$CODE_BUCKET" "ParameterKey=AgentCodeKey,ParameterValue=$KEY" \
    "ParameterKey=RcsAgentId,ParameterValue=$RCS_VALUE" \
    "ParameterKey=WhatsAppPhoneNumberId,ParameterValue=$WA_VALUE" \
    "ParameterKey=WhatsAppTemplateName,ParameterValue=$TEMPLATE_VALUE" \
    "ParameterKey=SesSender,ParameterValue=$SES_VALUE" \
    "ParameterKey=WindowSeconds,ParameterValue=${WINDOW_SECONDS:-90}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
API_URL="$(out ApiUrl)"; BUCKET="$(out DashboardBucket)"; DIST="$(out DashboardDistributionId)"; TOKEN="$(out StartToken)"

echo "==> publishing the dashboard"
printf '{"apiUrl": "%s", "noticeId": "HX200-2026-10", "startToken": "%s"}\n' "$API_URL" "$TOKEN" > .build/config.json
aws s3 sync dashboard/ "s3://$BUCKET/" --delete --only-show-errors
aws s3 cp .build/config.json "s3://$BUCKET/config.json" --only-show-errors --content-type application/json
aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/*' >/dev/null

echo
echo "Dashboard: $(out DashboardUrl)"
echo "API:       $API_URL"
echo "Next:      python seed/seed.py --stack $STACK   (then open the dashboard and press Start notice)"
