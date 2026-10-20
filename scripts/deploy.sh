#!/usr/bin/env bash
# One command: package the notice writer, build and deploy the stack, publish the dashboard.
#
#   scripts/deploy.sh
#
# Optional environment (empty until the matching identity exists; the stack deploys without them):
#   RCS_AGENT_ID  WHATSAPP_PHONE_NUMBER_ID  WHATSAPP_TEMPLATE_NAME  SES_SENDER  WINDOW_SECONDS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
STACK="${STACK:-proof-of-reach}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
CODE_BUCKET="por-agent-code-${ACCOUNT}-${REGION}"

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
aws s3api head-bucket --bucket "$CODE_BUCKET" --region "$REGION" 2>/dev/null \
  || aws s3api create-bucket --bucket "$CODE_BUCKET" --region "$REGION" >/dev/null
aws s3 cp "$ZIP" "s3://$CODE_BUCKET/$KEY" --only-show-errors

echo "==> sam build"
sam build >/dev/null

echo "==> sam deploy ($STACK)"
sam deploy --stack-name "$STACK" --region "$REGION" --resolve-s3 \
  --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides \
    "AgentCodeBucket=$CODE_BUCKET" "AgentCodeKey=$KEY" \
    "RcsAgentId=${RCS_AGENT_ID:-}" "WhatsAppPhoneNumberId=${WHATSAPP_PHONE_NUMBER_ID:-}" \
    "WhatsAppTemplateName=${WHATSAPP_TEMPLATE_NAME:-}" "SesSender=${SES_SENDER:-}" \
    "WindowSeconds=${WINDOW_SECONDS:-90}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
API_URL="$(out ApiUrl)"; BUCKET="$(out DashboardBucket)"; DIST="$(out DashboardDistributionId)"

echo "==> publishing the dashboard"
printf '{"apiUrl": "%s", "noticeId": "HX200-2026-10"}\n' "$API_URL" > .build/config.json
aws s3 sync dashboard/ "s3://$BUCKET/" --delete --only-show-errors
aws s3 cp .build/config.json "s3://$BUCKET/config.json" --only-show-errors --content-type application/json
aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/*' >/dev/null

echo
echo "Dashboard: $(out DashboardUrl)"
echo "API:       $API_URL"
echo "Next:      python seed/seed.py --stack $STACK   (then open the dashboard and press Start notice)"
