# Flight-data service answers for the enrichment tests

Written by hand to the shape AeroAPI v4 publishes for `GET /flights/{ident}` and
`GET /account/usage`. None was ever recorded from the service: every flight here is an
invented flight of the Sample family (DL 1234, MSP to PHL, 22 June 2027), and every
registration and identifier is made up.

| File | What it answers |
|---|---|
| `flights-icao.json` | the flight, asked by its ICAO designator (`DAL1234`) |
| `flights-iata.json` | the same flight, asked by its IATA code (`DL1234`) when AirTrail knows no ICAO designator for the airline |
| `flights-two.json` | two flights of that number between the same airports that day: no one answer |
| `flights-none.json` | no flight at all |
| `flights-evening.json` | the flight leaving at 19:30 Central time (after midnight UTC), with the same number the evening before and the evening after |
| `usage.json` | the account's calls and cost this period |
