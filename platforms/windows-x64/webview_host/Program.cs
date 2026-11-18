using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// 仅通过父进程的匿名管道交换数据，不开端口，不把凭据放进参数或日志。
internal static class Program
{
    internal static readonly object OutputLock = new();
    internal static void Send(object value)
    {
        lock (OutputLock) { Console.Out.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush(); }
    }
    [STAThread]
    static void Main()
    {
        Console.InputEncoding = Encoding.UTF8;
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            var line = Console.ReadLine();
            if (line == null || line.Length > 65536) return;
            var config = JsonDocument.Parse(line).RootElement.Clone();
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new BrowserForm(config));
        }
        catch (Exception e) { Send(new { type = "error", code = "browser_start", detail = e.GetType().Name }); }
    }
}

internal sealed class BrowserForm : Form
{
    readonly JsonElement config;
    readonly WebView2 view = new() { Dock = DockStyle.Fill };
    readonly string mode, target;
    bool ready;
    int payloadCount;
    [DllImport("user32.dll")] static extern nint SetParent(nint child, nint parent);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] static extern nint GetStyle(nint hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] static extern nint SetStyle(nint hwnd, int index, nint value);
    string TextValue(string key, string fallback = "") => config.TryGetProperty(key, out var v) ? v.GetString() ?? fallback : fallback;
    public BrowserForm(JsonElement input)
    {
        config = input; mode = TextValue("mode"); target = TextValue("tweet_id");
        Text = "XDownloader · WebView2";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        AutoScaleMode = AutoScaleMode.None;
        ClientSize = new Size(1000, 700);
        Controls.Add(view);
        view.Enter += (_, _) => { if (mode == "login") Program.Send(new { type = "browser_focus" }); };
        if (mode != "login") { Opacity = 0; Location = new Point(-20000, -20000); }
        Shown += async (_, _) => await Initialize();
        FormClosed += (_, _) => { view.Dispose(); Program.Send(new { type = "closed" }); };
    }
    void FocusBrowser()
    {
        if (mode != "login" || !ready || IsDisposed || !Visible) return;
        view.Focus();
    }
    protected override void OnGotFocus(EventArgs e)
    {
        base.OnGotFocus(e);
        FocusBrowser();
    }
    async Task Initialize()
    {
        try
        {
            if (mode == "login" && config.TryGetProperty("parent", out var parent))
            {
                // Keep WinForms managed window state consistent with WS_CHILD.
                TopLevel = false;
                SetParent(Handle, (nint)parent.GetInt64());
                SetStyle(Handle, -16, (nint)((GetStyle(Handle, -16).ToInt64() | 0x40000000L) & ~0x80000000L));
                Location = Point.Empty;
                Show();
            }
            var args = "--disable-features=msEdgeSidebarV2";
            var proxyMode = TextValue("proxy_mode", "system");
            if (proxyMode == "direct") args += " --no-proxy-server";
            else if (proxyMode == "manual") args += " --proxy-server=" + TextValue("proxy").Replace("socks5h://", "socks5://");
            var options = new CoreWebView2EnvironmentOptions(args) { Language = "zh-CN" };
            var env = await CoreWebView2Environment.CreateAsync(TextValue("runtime"), TextValue("profile"), options);
            await view.EnsureCoreWebView2Async(env);
            var core = view.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsWebMessageEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.ServerCertificateErrorDetected += (_, e) => e.Action = CoreWebView2ServerCertificateErrorAction.Cancel;
            core.NewWindowRequested += (_, e) => { e.Handled = true; if (Official(e.Uri)) core.Navigate(e.Uri); };
            core.NavigationStarting += (_, e) => { if (mode != "probe" && !Official(e.Uri)) e.Cancel = true; };
            core.ProcessFailed += (_, e) => Program.Send(new { type = "error", code = "browser_process", detail = e.ProcessFailedKind.ToString() });
            core.WebResourceResponseReceived += async (_, e) => await CaptureResponse(e);
            core.SourceChanged += (_, _) => Program.Send(new { type = "address", url = core.Source });
            core.NavigationCompleted += async (_, e) =>
            {
                Program.Send(new { type = "navigation", ok = e.IsSuccess, error = e.WebErrorStatus.ToString(), url = core.Source });
                if (e.IsSuccess && mode == "resolve")
                {
                    var raw = await core.ExecuteScriptAsync("document.documentElement.outerHTML");
                    var html = JsonSerializer.Deserialize<string>(raw);
                    if (html?.Length <= 8388608) Program.Send(new { type = "html", html });
                }
            };
            if (config.TryGetProperty("cookies", out var cookies))
                foreach (var item in cookies.EnumerateObject())
                    if (item.Name is "auth_token" or "ct0" or "twid")
                    {
                        var cookie = core.CookieManager.CreateCookie(item.Name, item.Value.GetString()!, ".x.com", "/");
                        cookie.IsSecure = true; cookie.IsHttpOnly = item.Name == "auth_token";
                        core.CookieManager.AddOrUpdateCookie(cookie);
                    }
            ready = true;
            Program.Send(new { type = "ready", version = env.BrowserVersionString });
            _ = Task.Run(ReadCommands);
            if (mode == "probe")
            {
                var file = TextValue("file");
                core.SetVirtualHostNameToFolderMapping("media.xdownloader.test", Path.GetDirectoryName(file)!, CoreWebView2HostResourceAccessKind.DenyCors);
                core.NavigateToString("<html><body><video autoplay muted controls src=\"https://media.xdownloader.test/" + Uri.EscapeDataString(Path.GetFileName(file)) + "\"></video></body></html>");
            }
            else core.Navigate(TextValue("url", "https://x.com/i/flow/login"));
        }
        catch (Exception e) { Program.Send(new { type = "error", code = "browser_init", detail = e.GetType().Name }); Close(); }
    }
    static bool Official(string url) => Uri.TryCreate(url, UriKind.Absolute, out var u) && u.Scheme == "https" && u.Host == "x.com" && u.IsDefaultPort && string.IsNullOrEmpty(u.UserInfo);
    bool Matches(string? text)
    {
        if (string.IsNullOrEmpty(text) || text.Length > 65536) return false;
        try
        {
            var v = JsonDocument.Parse(text).RootElement;
            if (v.TryGetProperty("variables", out var vars)) v = vars.ValueKind == JsonValueKind.String ? JsonDocument.Parse(vars.GetString()!).RootElement : vars;
            foreach (var key in new[] { "focalTweetId", "tweetId", "tweet_id" })
                if (v.TryGetProperty(key, out var id) && id.ToString() == target) return true;
        }
        catch (JsonException) { }
        return false;
    }
    static string? Variable(string query)
    {
        foreach (var pair in query.TrimStart('?').Split('&'))
        {
            var parts = pair.Split('=', 2);
            if (parts.Length == 2 && Uri.UnescapeDataString(parts[0]) == "variables") return Uri.UnescapeDataString(parts[1].Replace('+', ' '));
        }
        return null;
    }
    async Task CaptureResponse(CoreWebView2WebResourceResponseReceivedEventArgs e)
    {
        if (mode != "resolve" || payloadCount >= 8 || !Official(e.Request.Uri)) return;
        var uri = new Uri(e.Request.Uri);
        if (!uri.AbsolutePath.Contains("/graphql/")) return;
        try
        {
            var variables = Variable(uri.Query);
            bool matches = variables != null && Matches(variables);
            if (variables == null && e.Request.Method == "POST" && e.Request.Content is Stream content)
            {
                using var reader = new StreamReader(content, leaveOpen: true);
                var body = await reader.ReadToEndAsync();
                matches = Matches(body) || Matches(Variable(body));
            }
            if (!matches) return;
            var status = e.Response.StatusCode;
            if (status != 200) { Program.Send(new { type = "payload", status, payload = new { } }); return; }
            using var stream = await e.Response.GetContentAsync();
            using var buffer = new MemoryStream();
            var chunk = new byte[65536];
            int count;
            while ((count = await stream.ReadAsync(chunk)) > 0)
            {
                if (buffer.Length + count > 8388608) return;
                await buffer.WriteAsync(chunk.AsMemory(0, count));
            }
            var payload = JsonDocument.Parse(buffer.ToArray()).RootElement.Clone();
            payloadCount++;
            Program.Send(new { type = "payload", status, payload });
        }
        catch (Exception) { Program.Send(new { type = "capture_error" }); }
    }
    async Task ReadCommands()
    {
        try
        {
            string? line;
            while ((line = await Console.In.ReadLineAsync()) != null)
            {
                if (line.Length > 65536) continue;
                var command = JsonDocument.Parse(line).RootElement.Clone();
                BeginInvoke(async () => await Command(command));
            }
        }
        catch (Exception) { }
        if (!IsDisposed) BeginInvoke(Close);
    }
    async Task Command(JsonElement command)
    {
        if (!ready || IsDisposed) return;
        try
        {
            var core = view.CoreWebView2;
            switch (command.GetProperty("op").GetString())
            {
                case "focus": FocusBrowser(); break;
                case "close": Close(); break;
                case "resize": ClientSize = new Size(command.GetProperty("width").GetInt32(), command.GetProperty("height").GetInt32()); Location = Point.Empty; break;
                case "reload": core.Reload(); break;
                case "navigate": var url = command.GetProperty("url").GetString()!; if (Official(url)) core.Navigate(url); break;
                case "cookies":
                    var saved = new Dictionary<string, string>();
                    foreach (var cookie in await core.CookieManager.GetCookiesAsync("https://x.com"))
                        if (cookie.Domain.TrimStart('.') == "x.com" && cookie.Name is "auth_token" or "ct0" or "twid") saved[cookie.Name] = cookie.Value;
                    Program.Send(new { type = "cookies", cookies = saved }); break;
                case "probe":
                    var probe = await core.ExecuteScriptAsync("JSON.stringify({h264:document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.42E01E\"'),aac:document.createElement('video').canPlayType('audio/mp4; codecs=\"mp4a.40.2\"'),videos:Array.from(document.querySelectorAll('video')).map(v=>({duration:v.duration,time:v.currentTime,paused:v.paused,ready:v.readyState,error:v.error?.code||0}))})");
                    Program.Send(new { type = "probe", result = JsonSerializer.Deserialize<string>(probe) }); break;
            }
        }
        catch (Exception e) { Program.Send(new { type = "error", code = "browser_command", detail = e.GetType().Name }); }
    }
}
