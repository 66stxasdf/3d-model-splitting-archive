Option Explicit
' Multi-model agent workbench launcher (ASCII only on purpose)
'   1) if another program is squatting on 8765, kill it first
'   2) start our server silently with pythonw
'   3) open the page in the default browser
Dim URL, PYW, PY, SRV, ENSURE, LOG, PORT, SH, PROFILE, Q, CMD
Dim OK, I

Q   = Chr(34)
URL = "http://127.0.0.1:8765/"
PORT = "8765"
SRV = "D:\workbench\server.py"
ENSURE = "D:\workbench\ensure_port.py"
LOG = "D:\workbench\ensure_port.log"

Set SH = CreateObject("WScript.Shell")
PROFILE = SH.ExpandEnvironmentStrings("%USERPROFILE%")
PYW = PROFILE & "\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
PY  = PROFILE & "\.workbuddy\binaries\python\versions\3.13.12\python.exe"

Function ServerUp()
    Dim h, r
    r = False
    On Error Resume Next
    Set h = CreateObject("MSXML2.XMLHTTP")
    h.Open "GET", URL & "api/graph", False
    h.Send
    If Err.Number = 0 And h.Status = 200 Then r = True
    Err.Clear
    On Error GoTo 0
    ServerUp = r
End Function

OK = ServerUp()

If Not OK Then
    SH.CurrentDirectory = "D:\workbench"
    ' Step 1: clear any foreign listener on 8765. Use console python.exe so
    ' cmd /c actually waits for it (pythonw.exe is GUI-subsystem, never waited on).
    CMD = "cmd /c " & Q & Q & PY & Q & " " & Q & ENSURE & Q & " " & PORT & " > " & Q & LOG & Q & " 2>&1" & Q
    SH.Run CMD, 0, True
    ' Step 2: start our server, which binds the port exclusively.
    SH.Run Q & PYW & Q & " " & Q & SRV & Q & " " & PORT, 0, False
    For I = 1 To 40
        WScript.Sleep 500
        If ServerUp() Then
            OK = True
            Exit For
        End If
    Next
End If

If OK Then
    SH.Run URL, 1, False
Else
    MsgBox "Workbench server did not start in 20s." & vbCrLf & _
           "Check D:\workbench\ensure_port.log and stderr.log", 48, "Workbench"
End If
