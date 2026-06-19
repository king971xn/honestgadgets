Add-Type -AssemblyName System.Windows.Forms
$url = "https://gitee.com/qh971xnzg/honestgadgets/pages"
$wb = New-Object -ComObject "InternetExplorer.Application"
$wb.Visible = $true
$wb.Navigate2($url)
while ($wb.Busy) { [System.Threading.Thread]::Sleep(200) }
[System.Threading.Thread]::Sleep(3000)

# Get the document
$doc = $wb.Document

# Log all input elements
$inputs = $doc.getElementsByTagName("input")
foreach ($inp in $inputs) {
    $name = $inp.getAttribute("name")
    $type = $inp.getAttribute("type")
    $placeholder = $inp.getAttribute("placeholder")
    $id = $inp.id
    Write-Output "INPUT: id=$id name=$name type=$type placeholder=$placeholder"
}

# Log all select elements
$selects = $doc.getElementsByTagName("select")
foreach ($sel in $selects) {
    Write-Output "SELECT: id=$($sel.id) name=$($sel.getAttribute('name'))"
}

# Log all buttons
$buttons = $doc.getElementsByTagName("button")
foreach ($btn in $buttons) {
    Write-Output "BUTTON: $($btn.innerText) id=$($btn.id)"
}

Write-Output "SCRIPT_DONE"
$wb.Quit()
