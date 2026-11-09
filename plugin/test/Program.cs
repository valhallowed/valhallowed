using Valhallowed.Plugin;

// Checks the plugin's telemetry against B2. The contract check: a batch
// written for a fixed session and time must equal args[0], an example
// scripts/check-schema.sh validates against contracts/intake.events.schema.json,
// byte for byte, so what the plugin writes is what the contract allows.
if (args.Length != 1)
{
    Console.Error.WriteLine("usage: Valhallowed.Plugin.Test EXAMPLE");
    return 2;
}

var failures = new List<string>();
void Check(bool ok, string what)
{
    if (!ok)
    {
        failures.Add(what);
    }
}

const string Session = "0b6f2a4e-5d1c-4e8a-9f3b-7c2d1e0a9b84";
const string Player = "Steam_76561190000000001";
DateTime at = new DateTime(2026, 1, 1, 12, 0, 0, DateTimeKind.Utc).AddTicks(1234560);
DateTime sentAt = new DateTime(2026, 1, 1, 12, 0, 5, DateTimeKind.Utc);

// The contract.
string written = Batch.Envelope(Session, sentAt, new[]
{
    Batch.SessionStart(at),
    Batch.Join(at, Player, "Ann \"the\" \\ Bold"),
    Batch.Position(at, Player, 1.5f, 30f, -2.25f),
    Batch.Leave(at, Player),
});
string example = File.ReadAllText(args[0]).TrimEnd('\n');
Check(written == example, $"the batch the plugin writes is not {args[0]}:\n{written}");

// Players.
Check(Players.SteamId("76561190000000001") == Player, "a Steam host name gives the game's platform id");
Check(Players.SteamId("") == null, "an empty host name gives no id");
Check(Players.SteamId("7656 1190") == null, "an id outside B2's pattern is refused");
Check(Players.SteamId(new string('1', 59)) == null, "an id over 64 characters is refused");
Check(Players.Name("Ann\u0007\nBold\u007f") == "AnnBold", "control characters leave a name");
Check(Players.Name("\u0001\u0002") == null, "a name of control characters is no name");
Check(Players.Name(new string('a', 63) + "\U0001F600") == new string('a', 63) + "\U0001F600",
    "a name of 64 characters keeps its last surrogate pair");
Check(Players.Name(new string('a', 70)) == new string('a', 64), "a name is cut to 64 characters");
Check(!Batch.IsCoordinate(float.NaN) && !Batch.IsCoordinate(float.PositiveInfinity)
    && !Batch.IsCoordinate(-100001f) && Batch.IsCoordinate(-100000f), "coordinates are finite and in B2's bounds");

// The queue drops and counts when full, and logs once per spell of being full.
var warnings = new List<string>();
var queue = new EventQueue(2, warnings.Add);
queue.Add("a");
queue.Add("b");
queue.Add("c");
queue.Add("d");
Check(queue.Dropped == 2 && warnings.Count == 1, "a full queue drops and counts, and logs once");
Check(string.Join(",", queue.Take(10)) == "a,b", "the queue keeps the oldest events");
queue.Add("e");
queue.Add("f");
queue.Add("g");
Check(queue.Dropped == 3 && warnings.Count == 2, "a queue full again logs again");

// The sender carries session.start until a batch is accepted, splits a long
// queue into batches of B2's most, and logs a failure once and the recovery.
var posted = new List<string>();
string refuse = "intake answered 503";
var logs = new List<string>();
var events = new EventQueue(2000, logs.Add);
var sender = new Sender(events, Session, Batch.SessionStart(at), body =>
{
    posted.Add(body);
    return refuse;
}, logs.Add, () => sentAt);
string start = Batch.SessionStart(at);

sender.SendQueued();
sender.SendQueued();
Check(posted.Count == 2 && posted.TrueForAll(b => b.Contains(start)) && logs.Count == 1,
    "an empty queue still sends session.start, and a failure logs once");

refuse = null;
for (int i = 0; i < 1000; i++)
{
    events.Add(Batch.Leave(at, Player));
}
posted.Clear();
sender.SendQueued();
Check(posted.Count == 3, $"1000 events and the start go in three batches, not {posted.Count}");
Check(posted.Count > 0 && posted[0].Contains(start) && !posted.Skip(1).Any(b => b.Contains(start)),
    "session.start leads the first accepted batch only");
Check(logs.Count == 2 && logs[1].Contains("2 batches dropped"), "recovery logs the batches dropped");

posted.Clear();
sender.SendQueued();
Check(posted.Count == 0, "once accepted, an empty queue sends nothing");

foreach (string failure in failures)
{
    Console.Error.WriteLine($"plugin test: {failure}");
}
if (failures.Count > 0)
{
    return 1;
}
Console.WriteLine("plugin test: the telemetry holds to B2");
return 0;
