# Answer a native dialog the Markdown Sidekick window opened (its own
# process only): wait for it, optionally type a path into its file-name box,
# then click a button (1 = Save / Open / Select Folder / OK, 2 = Cancel,
# 6 = Yes, 7 = No). Exit 0 = clicked, 1 = no window or no dialog in time,
# 2 = several dialogs could be meant (pass -Title).
#
# With several dialogs open, the one that takes input wins: a modal child
# (Save As's "already exists - replace?") disables its owner. -Title narrows
# the choice to dialogs whose title contains it.
param([string]$Path = "", [int]$Button = 1, [int]$TimeoutS = 20, [string]$Title = "")

Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class W {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h, int id);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, string l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, StringBuilder l);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static string Cls(IntPtr h){var s=new StringBuilder(256);GetClassName(h,s,256);return s.ToString();}
  public static string Txt(IntPtr h){var s=new StringBuilder(512);GetWindowText(h,s,512);return s.ToString();}
  // GetWindowText can't read another process's edit box; WM_GETTEXT can.
  public static string EditText(IntPtr h){var s=new StringBuilder(1024);SendMessage(h,0x000D,(IntPtr)1024,s);return s.ToString();}
  public static List<IntPtr> Top(){var r=new List<IntPtr>();EnumWindows((h,l)=>{r.Add(h);return true;},IntPtr.Zero);return r;}
  public static List<IntPtr> Kids(IntPtr p){var r=new List<IntPtr>();EnumChildWindows(p,(h,l)=>{r.Add(h);return true;},IntPtr.Zero);return r;}
  public static uint Pid(IntPtr h){uint p;GetWindowThreadProcessId(h,out p);return p;}
}
"@

$main = [W]::Top() | Where-Object { [W]::IsWindowVisible($_) -and [W]::Txt($_).StartsWith("Markdown Sidekick") -and [W]::Cls($_) -ne "#32770" } | Select-Object -First 1
if (-not $main) { Write-Output "no Markdown Sidekick window"; exit 1 }
$appPid = [W]::Pid($main)
$deadline = (Get-Date).AddSeconds($TimeoutS)
$found = @()
while (-not $found -and (Get-Date) -lt $deadline) {
  $found = @([W]::Top() | Where-Object { [W]::Cls($_) -eq "#32770" -and [W]::IsWindowVisible($_) -and [W]::Pid($_) -eq $appPid })
  if ($Title) { $found = @($found | Where-Object { [W]::Txt($_) -like "*$Title*" }) }
  if (-not $found) { Start-Sleep -Milliseconds 200 }
}
if (-not $found) { Write-Output ("no dialog from pid $appPid" + $(if ($Title) { " titled like '$Title'" })); exit 1 }
$ready = @($found | Where-Object { [W]::IsWindowEnabled($_) })
if ($ready.Count -ne 1) {
  Write-Output ("several dialogs could be meant: " + (($found | ForEach-Object { "'" + [W]::Txt($_) + "'" }) -join ", ") + " - pass -Title")
  exit 2
}
$dlg = $ready[0]
$texts = [W]::Kids($dlg) | Where-Object { [W]::Cls($_) -in @("Static", "Button", "DirectUIHWND") } | ForEach-Object { [W]::Txt($_) } | Where-Object { $_ }
$others = $found.Count - 1
Write-Output ("dialog: '" + [W]::Txt($dlg) + "' | " + (($texts | Select-Object -First 6) -join " | ") + $(if ($others) { " (+$others waiting behind it)" }))
if ($Path) {
  $box = [W]::GetDlgItem($dlg, 1148)  # the file-name combo (cmb13)
  $edit = $null
  if ($box -ne [IntPtr]::Zero) { $edit = [W]::Kids($box) | Where-Object { [W]::Cls($_) -eq "Edit" } | Select-Object -First 1 }
  if (-not $edit) { $edit = [W]::Kids($dlg) | Where-Object { [W]::Cls($_) -eq "Edit" } | Select-Object -First 1 }
  if (-not $edit) { Write-Output "this dialog has no file-name box"; exit 1 }
  [void][W]::SendMessage($edit, 0x000C, [IntPtr]::Zero, $Path)  # WM_SETTEXT
  Write-Output ("typed: " + [W]::EditText($edit))
}
$btn = [W]::GetDlgItem($dlg, $Button)
$labels = @{ 1 = @("OK", "Save", "Open", "Select Folder"); 2 = @("Cancel"); 6 = @("Yes"); 7 = @("No") }
if ($btn -eq [IntPtr]::Zero -and $labels.ContainsKey($Button)) {
  # Task-dialog buttons (Save As's "replace?" prompt) have no control ID: find them by label.
  $btn = [W]::Kids($dlg) | Where-Object { [W]::Cls($_) -eq "Button" -and $labels[$Button] -contains ([W]::Txt($_) -replace "&", "") } | Select-Object -First 1
}
if (-not $btn -or $btn -eq [IntPtr]::Zero) { Write-Output "this dialog has no button $Button"; exit 1 }
Write-Output ("clicking: '" + [W]::Txt($btn) + "'")
[void][W]::PostMessage($btn, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)  # BM_CLICK
