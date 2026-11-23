# Models

tripbox reads an email in two steps. A deterministic extractor (KDE's kitinerary) reads emails
that carry structured data, with no model. A model reads what the extractor could not. You can run
tripbox with no model at all: those emails then wait, kept, until one is set up. Many emails carry
no structured data, and a forward made by hand drops what there was, so with no model the mail
should arrive by a Gmail filter, as an attachment, or as an uploaded `.eml` of the original (see
[mail](mail.md#with-no-model)); on the corpus below the extractor reads only a few of the emails.

The settings are in [configuration](configuration.md#the-model). What leaves your computer is in
[privacy](privacy.md).

## Providers

`TRIPBOX_LLM_PROVIDER` picks who reads.

- **`anthropic`** (the default) with `TRIPBOX_LLM_API_KEY`. The model is `TRIPBOX_LLM_MODEL`,
  `claude-haiku-5-5` unless you change it. It is sent the email's text and, from the email's
  attachments, up to three PDFs of at most 5 MB each.
- **`openai`**: OpenAI's own service, or any server that speaks its Chat Completions API (one you
  run yourself, such as Ollama). Set `TRIPBOX_LLM_MODEL` to a model the server serves, and
  `TRIPBOX_LLM_BASE_URL` for a server of your own. It is sent text only, never an attachment: a
  PDF that holds the booking is not read by it. tripbox asks for a JSON answer by schema first
  and, for a server that rejects that, in JSON mode. OpenAI's own service is sent
  `max_completion_tokens`, another server `max_tokens`. A login in the address is sent as Basic
  authentication in place of a key. The client library also reads `OPENAI_ORG_ID`,
  `OPENAI_PROJECT_ID` and `OPENAI_CUSTOM_HEADERS` from the environment: they are not tripbox
  settings, so `.env.example` and the settings reference leave them out and `doctor` does not
  report them (it flags only unknown `TRIPBOX_` names). A line for one in `.env` reaches the
  worker and the bot, which read the whole file.
- **`none`** keeps the model off whatever else is set.

## What is asked, and how a wrong answer is kept out

The model is asked for a fixed shape of answer, and tripbox checks that answer: every field is
optional, an airport is checked against the table that ships with tripbox, a time must be a time. A
field the email does not print stays empty; the model is told to leave it so. What fails a check
is marked on the page as **To check**, never trusted. The email is data: the instructions to the
model say so, but that has not been tried against a real model (see below).

An answer that the model refuses, cuts short, or gives in the wrong shape fails that message
once, and so does a request the model's server refuses (a request too large, say). It is kept,
and `tripbox replay --reread --status failed` asks again. A model name the server does not know
(a retired model, a typo) fails nothing: mail waits, as while the model cannot be reached, until
`TRIPBOX_LLM_MODEL` names one it knows.

## The cap

Every call to the configured model is counted before it is made.

- A model whose price is known stops once this month's calls cost `TRIPBOX_LLM_MONTHLY_BUDGET_USD`
  (default 5). tripbox ships the price of `claude-haiku-5-5` (a dated name of it too) in
  `src/tripbox/lists/prices.yaml`, including its higher price for a prompt of over 100,000 tokens,
  which bills the whole call; for any other model, give
  `TRIPBOX_LLM_PRICE_USD_PER_MTOK` as `INPUT,OUTPUT`.
- A model with no known price stops once today's calls reach `TRIPBOX_LLM_MAX_CALLS_PER_DAY`
  (default 100).
- Months and days are UTC. The cap is checked before a call, so one call may pass it. A budget of
  `0` means the model is never asked.
- An answer that is read back (a replay, or one stored with its message) is never asked again, so
  it is never counted.

Mail that cannot be read now (no model, the cap reached, the model not answering) waits, and the
first poll that can read it does, oldest first. So does mail the extractor read only in part:
nothing of that part is filed until the model has read the whole email. You are told once a month or day for the cap and
once for an outage. After an outage, no call is made for a minute, so the rest of a poll's mail
waits at once instead of each message timing out.

## Reading how a model does: `tripbox eval`

`tripbox eval` files a corpus of 30 invented emails, in order, into an empty ledger of its own,
with the model you have set up, and scores what it read and what filing did. It uses none of your
settings but the model's, and writes nothing of yours. `tripbox eval --recorded` answers from the
corpus's own saved answers instead and needs no model; that is how it runs here. `--json FILE`
saves the scorecard. The exit code is 0 only when every email is right.

`tripbox eval` without `--recorded` asks the model and spends what that costs on your key. Its
calls are not counted in your monthly cap.

The scorecard, as printed, with each line explained:

```
tripbox eval: 30 emails from /app/tests/corpus (wire schema 1, answers: recorded)
ok    01-markup-flight            kitinerary  17/17
ok    21-stranger                 -           -
parsers: kitinerary: 3 model: 26 none: 1
answers: hand-written: 26 recorded: 0 not asked: 4
fields: 347 of 347 right (100.0%)
known gaps: 0
ledger: 12 trips, 32 bookings, 1 unsorted
eval: 30 of 30 emails right
```

- The first line gives the number of emails, the corpus's folder, the version of the answer's
  shape, and where the answers came from: `recorded` (the corpus's own), `model` (a model was
  asked; `--json` saves its name) or `none` (no model is set up).
- Each email has a line: `ok` or `FAIL`, its name, who read it (`kitinerary`, `model`, or `-`
  when no reader was asked, as for an email from a sender that is not allowed), and how many
  of its fields were right of how many there are (`-` when none were expected). Under a failing
  one come what was wanted and what was read.
- `parsers:` counts the emails each reader read; `none` is the ones nothing read.
- `answers:` counts the corpus's answers by where they came from: `hand-written`, `recorded` from
  a real model, and `not asked`, for emails no model is asked about. Every answer in the corpus is
  hand-written; none was recorded from a real model.
- `fields:` is the total of fields right, and its share.
- `known gaps:` is the number of emails whose filing is not yet what it is meant to be, as the
  corpus records it. It is 0 now.
- `ledger:` is what filing the whole corpus leaves: the trips the page would list, the bookings
  in them, and the bookings with no date (`unsorted`).
- `model cost:` (only when a model was asked) is what the run cost, at the price tripbox knows.
- The last line is how many emails are right, out of how many.

## What has not been tried

tripbox has not been run against a real model's answers on the corpus: its answers are written
by hand. The sentence that tells the model the email is data, the OpenAI-compatible provider
against a real server, and a local model are all untried. A text-only model cannot read a PDF
booking.
