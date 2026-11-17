using Valhallowed.Plugin;

// Checks the plugin's telemetry against B2 and its map export against B5. The
// contract checks: a batch written for a fixed session and time must equal
// args[0], an example scripts/check-schema.sh validates against
// contracts/intake.events.schema.json, byte for byte; and the export written
// from a synthetic sampler must equal args[1], the same kind of example for
// contracts/plugin.map-export.schema.json, and the raster beside it, whose
// name is args[1] with .json replaced by .raster.bin. So what the plugin
// writes is what the contract allows.
if (args.Length != 2 || !args[1].EndsWith(".json"))
{
    Console.Error.WriteLine("usage: Valhallowed.Plugin.Test BATCH_EXAMPLE MAP_EXPORT_EXAMPLE");
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
Check(Players.PositionDue(false, 0f, 0f, 2f), "a player's first position is due");
Check(!Players.PositionDue(true, 2.9f, 10f, 2f), "a player who moved less than 3 m, 10 s ago, sends nothing");
Check(Players.PositionDue(true, 3f, 0f, 2f), "a player who moved 3 m sends their position");
Check(!Players.PositionDue(true, 0f, 28f, 2f), "a still player's position 28 s old waits for the next sample");
Check(Players.PositionDue(true, 0f, 28.1f, 2f), "a still player's position that would be over 30 s old by the next sample is sent");
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

// B5: the map export. The synthetic sampler is exact in 32-bit floats, so the
// example's bytes do not depend on the machine.
var world = new WorldIdentity("Meadowfold", "Hildir", 1234567, 2, 30f);
Sample Synthetic(float x, float z)
{
    float height = 30f + (x + z) / 256f;
    ushort biome = height <= 30f ? (ushort)256 : z > 5000f ? (ushort)4 : x < -4000f ? (ushort)8
        : x > 5000f ? (ushort)16 : z < -5000f ? (ushort)2 : (ushort)1;
    return new Sample(biome, height, 0.75f + (x + 10500f) / 32768f);
}

ExportFiles export = MapExport.Build(world, 16, 10500f, Synthetic, CancellationToken.None);
string rasterExample = args[1].Substring(0, args[1].Length - ".json".Length) + ".raster.bin";
Check(export.Meta.AsSpan().SequenceEqual(File.ReadAllBytes(args[1])),
    $"the meta the plugin writes is not {args[1]}:\n{System.Text.Encoding.UTF8.GetString(export.Meta)}");
Check(export.Raster.AsSpan().SequenceEqual(File.ReadAllBytes(rasterExample)), $"the raster the plugin writes is not {rasterExample}");
Check(export.Raster.Length == 16 * 16 * 10, "a raster is size * size * 10 bytes");

// Row 0 is north and column 0 is west: pixel (0, 0) samples (-9843.75, 9843.75) and
// pixel (15, 15) samples (9843.75, -9843.75).
var sampled = new List<(float, float)>();
MapExport.Build(world, 16, 10500f, (x, z) => { sampled.Add((x, z)); return Synthetic(x, z); }, CancellationToken.None);
Check(sampled.Count == 256 && sampled[0] == (-9843.75f, 9843.75f) && sampled[1] == (-8531.25f, 9843.75f)
    && sampled[16] == (-9843.75f, 8531.25f) && sampled[255] == (9843.75f, -9843.75f),
    "pixels are sampled at their centres, row by row, from the north-west");

Check(Throws<InvalidOperationException>(() => MapExport.Build(world, 2, 10f, (x, z) => new Sample(3, 0f, 0f), CancellationToken.None)),
    "a biome B5 does not name is refused");
Check(Throws<ArgumentException>(() => MapExport.Build(world, 0, 10f, Synthetic, CancellationToken.None)), "an empty raster is refused");
Check(Throws<ArgumentException>(() => MapExport.Build(world, 2, 0f, Synthetic, CancellationToken.None)), "a raster of no extent is refused");
Check(Throws<ArgumentException>(() => MapExport.Build(new WorldIdentity("", "", 0, 2, 30f), 2, 10f, Synthetic, CancellationToken.None)),
    "a world with no name is refused");
using (var cancelled = new CancellationTokenSource())
{
    cancelled.Cancel();
    Check(Throws<OperationCanceledException>(() => MapExport.Build(world, 2, 10f, Synthetic, cancelled.Token)), "a cancelled build stops");
}

string directory = Path.Combine(Path.GetTempPath(), "vhd-plugin-test-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(directory);
try
{
    Check(!MapExport.IsCurrent(directory, world, 16, 10500f), "an empty directory holds no current export");
    MapExport.Write(directory, export);
    Check(Directory.GetFileSystemEntries(directory).Select(Path.GetFileName).OrderBy(n => n).SequenceEqual(new[] { "meta.json", "raster.bin" }),
        "a written export is the two files and no temporary file");
    Check(File.ReadAllBytes(Path.Combine(directory, "meta.json")).AsSpan().SequenceEqual(export.Meta)
        && File.ReadAllBytes(Path.Combine(directory, "raster.bin")).AsSpan().SequenceEqual(export.Raster), "the files hold the bytes built");
    Check(MapExport.IsCurrent(directory, world, 16, 10500f), "a written export is current");
    MapExport.Write(directory, export);
    Check(MapExport.IsCurrent(directory, world, 16, 10500f), "an export written over itself is current");
    Check(!MapExport.IsCurrent(directory, new WorldIdentity("Meadowfold", "Hildir", 1234568, 2, 30f), 16, 10500f), "another seed is not current");
    Check(!MapExport.IsCurrent(directory, new WorldIdentity("Meadowfold", "Other", 1234567, 2, 30f), 16, 10500f), "another seed name is not current");
    Check(!MapExport.IsCurrent(directory, new WorldIdentity("Meadowfold", "Hildir", 1234567, 3, 30f), 16, 10500f), "another generator version is not current");
    Check(!MapExport.IsCurrent(directory, new WorldIdentity("Another", "Hildir", 1234567, 2, 30f), 16, 10500f), "another world is not current");
    Check(!MapExport.IsCurrent(directory, new WorldIdentity("Meadowfold", "Hildir", 1234567, 2, 31f), 16, 10500f), "another water level is not current");
    Check(!MapExport.IsCurrent(directory, world, 32, 10500f), "another resolution is not current");
    Check(!MapExport.IsCurrent(directory, world, 16, 10000f), "another extent is not current");

    string rasterPath = Path.Combine(directory, "raster.bin");
    byte[] changed = (byte[])export.Raster.Clone();
    changed[100] ^= 1;
    File.WriteAllBytes(rasterPath, changed);
    Check(!MapExport.IsCurrent(directory, world, 16, 10500f), "a raster that is not the meta's is not current");
    File.WriteAllBytes(rasterPath, export.Raster.AsSpan(0, 100).ToArray());
    Check(!MapExport.IsCurrent(directory, world, 16, 10500f), "a short raster is not current");
    File.Delete(rasterPath);
    Check(!MapExport.IsCurrent(directory, world, 16, 10500f), "a missing raster is not current");

    File.WriteAllBytes(rasterPath, export.Raster);
    string metaPath = Path.Combine(directory, "meta.json");
    string meta = File.ReadAllText(metaPath);
    foreach (string broken in new[] { "", "{", meta.Replace("\"bytes\":2560", "\"bytes\":2561"), meta.TrimEnd('\n'),
        meta.Substring(0, meta.IndexOf(",\"sha256\"")) + ",\"sha256\":\"}}\n", meta.Replace("\"sha256\":\"8", "\"sha256\":\"9") })
    {
        File.WriteAllText(metaPath, broken);
        Check(!MapExport.IsCurrent(directory, world, 16, 10500f), $"a meta that is not the export's is not current: {broken}");
    }
    File.WriteAllText(metaPath, new string('x', 5000));
    Check(!MapExport.IsCurrent(directory, world, 16, 10500f), "an oversized meta is not current");
    File.WriteAllText(metaPath, meta);
    Check(MapExport.IsCurrent(directory, world, 16, 10500f), "the meta put back is current");

    // The meta goes last: a meta that cannot be replaced leaves the new raster
    // in place and no temporary file.
    File.Delete(metaPath);
    Directory.CreateDirectory(metaPath);
    var other = MapExport.Build(world, 4, 10500f, Synthetic, CancellationToken.None);
    Check(Throws<IOException>(() => MapExport.Write(directory, other)), "a failed write is reported");
    Check(File.ReadAllBytes(rasterPath).AsSpan().SequenceEqual(other.Raster), "the raster is written before the meta");
    Check(!File.Exists(Path.Combine(directory, ".meta.json.tmp")), "a failed write leaves no temporary file");
}
finally
{
    Directory.Delete(directory, true);
}
string missing = Path.Combine(Path.GetTempPath(), "vhd-plugin-test-" + Guid.NewGuid().ToString("N"));
Check(Throws<IOException>(() => MapExport.Write(missing, export)) && !Directory.Exists(missing), "a missing directory is never made");
Check(!MapExport.IsCurrent(missing, world, 16, 10500f), "a missing directory holds no current export");

static bool Throws<T>(Action action) where T : Exception
{
    try
    {
        action();
        return false;
    }
    catch (T)
    {
        return true;
    }
}

foreach (string failure in failures)
{
    Console.Error.WriteLine($"plugin test: {failure}");
}
if (failures.Count > 0)
{
    return 1;
}
Console.WriteLine("plugin test: the telemetry holds to B2 and the map export to B5");
return 0;
