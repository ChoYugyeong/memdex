using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using System.Reflection;

[assembly: AssemblyTitle("Memdex Studio")]
[assembly: AssemblyProduct("Memdex Studio")]

internal static class Studio
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length > 1)
                throw new ArgumentException("Open one project folder at a time.");
            string runtime = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime", "pythonw.exe");
            string command = "-I -m memdex.desktop";
            if (args.Length == 1)
            {
                if (!Directory.Exists(args[0]))
                    throw new DirectoryNotFoundException("Project folder does not exist: " + args[0]);
                // End the quoted path in a dot so a root's final backslash stays literal.
                command += " -- \"" + Path.Combine(Path.GetFullPath(args[0]), ".") + "\"";
            }
            Process.Start(new ProcessStartInfo(runtime, command) { UseShellExecute = false });
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Memdex Studio", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
