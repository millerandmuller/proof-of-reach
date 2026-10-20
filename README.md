# Proof of Reach

Sent is not proof. Proof of Reach pursues a mandatory notice (a product recall, a
breach notification, a fee notice) across RCS, WhatsApp and email until every
person has provably acknowledged it, and shows exactly who still does not know.

Status: under construction for the AWS Communication Developer Services Agentic AI
Partner Hackathon. This README is completed before submission.

## Layout

- `src/common`: ledger (DynamoDB), proof tiers, payload normalizers, `PutMessageFeedback` loop closer
- `src/consumers`: four SNS consumers (RCS events, RCS two-way, WhatsApp, SES)
- `src/senders`: the three CDS runtime calls (`SendRcsMessage`, `SendWhatsAppMessage`, SES `SendEmail`)
- `src/engine`: deterministic escalation ladder
- `tests`: run with `PYTHONPATH=src python -m pytest tests`

License: MIT
