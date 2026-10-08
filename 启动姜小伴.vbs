Option Explicit
Dim shell, fs, folder, python, candidates, p
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
python = "pythonw.exe"
candidates = Array(folder & "\.venv\Scripts\pythonw.exe", fs.GetParentFolderName(folder) & "\.venv\Scripts\pythonw.exe", "E:\python\pythonw.exe")
For Each p In candidates
    If fs.FileExists(p) Then
        python = p
        Exit For
    End If
Next
On Error Resume Next
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & folder & "\ginger_app.py" & Chr(34), 0, False
If Err.Number <> 0 Then
    MsgBox "Python 3.10+ is required. Open ginger_app.py in PyCharm and click Run.", 48, "Ginger Companion"
End If
