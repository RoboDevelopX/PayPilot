# Demo video script (about 2:30)

Record in the real sandbox mode (header shows **PayPal sandbox · live** and the Claude model). Have a second browser tab logged in to [sandbox.paypal.com](https://www.sandbox.paypal.com) with your sandbox **Personal** account, and use that account's email as the client below (shown here as `client@personal.example.com`). Set L to $50 and the daily budget to $150 before you start. Use no music you don't own.

| Time | Show | Say |
|---|---|---|
| 0:00 | The PayPilot screen | "Handing an AI your wallet feels risky, and chasing payments by hand is tedious. PayPilot is an AI agent that does PayPal tasks for you, inside rules you set." |
| 0:15 | Rules panel, point at L and the formula | "My rule: anything up to fifty dollars goes through on its own. Anything above it waits for me. The AI can't change these rules." |
| 0:25 | Type: *Bill client@personal.example.com for 3 hours of consulting at $120 and a $40 travel fee, due in 30 days* | "I just describe the job. Claude turns it into a structured invoice with two line items, and PayPilot sends it through the PayPal Invoicing API." |
| 0:45 | Invoices tab, then the sandbox inbox or the invoice in sandbox.paypal.com | "That's a real PayPal sandbox invoice." |
| 0:55 | Type: *Pay client@personal.example.com $25 for lunch* | "Twenty-five dollars is under my limit, so it's paid immediately with PayPal Payouts." |
| 1:10 | Type: *Send client@personal.example.com $400 for the photo shoot* | "Four hundred is over the limit. Claude proposed it, but the code said no, not without me. Nothing has been sent." |
| 1:25 | Approvals tab: read the reasons, click Approve | "Here's exactly why it's waiting. I approve, and only now does the money move." |
| 1:40 | Type: *Pay someone@example.com $5,000* | "Five thousand is above my hard cap, so it's blocked even if I wanted to approve it." |
| 1:50 | Click **Run follow-ups now**, show the reminder text | "For unpaid invoices, PayPilot follows up on its own. Claude writes each reminder, a little firmer every time, and sends it through PayPal." |
| 2:05 | Pay the invoice in the sandbox Personal account, then **Refresh status** | "When the client pays, PayPilot sees it in PayPal and stops chasing." |
| 2:15 | Audit log tab | "Every proposal and every decision is logged. The AI proposes, the code disposes." |
| 2:25 | Back to the full screen | "PayPilot: an AI co-pilot for getting paid and paying smart, built on PayPal and Claude." |

Tips: keep the browser zoom at 110–125% so text is readable, and trim pauses while Claude is thinking.
