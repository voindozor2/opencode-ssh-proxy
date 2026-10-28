// Compiled locally by Windows PowerShell/.NET. No downloads or credentials.
using System;
using System.Diagnostics;
using System.Text;

public static class OpenCodeProxyCommand {
    // Windows argv quoting, including embedded quotes and trailing backslashes.
    private static string Quote(string value) {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
            } else {
                result.Append('\\', slashes);
                result.Append(c);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    public static int Main(string[] args) {
        try {
            var line = new StringBuilder();
            line.Append(Quote(@"@@LAUNCHER@@"));
            line.Append(' ').Append(Quote(@"@@CONFIG@@"));
            foreach (string arg in args) { line.Append(' ').Append(Quote(arg)); }
            var start = new ProcessStartInfo(@"@@PYTHON@@", line.ToString());
            start.UseShellExecute = false;
            // Inherit console I/O and cwd; no environment mutation here.
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
            using (var child = Process.Start(start)) {
                child.WaitForExit();
                return child.ExitCode;
            }
        } catch {
            Console.Error.WriteLine("OpenCode proxy command could not launch Python. Private details suppressed.");
            return 1;
        }
    }
}
