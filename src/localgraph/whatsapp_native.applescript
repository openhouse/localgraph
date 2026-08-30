use framework "Foundation"
use scripting additions

-- No coordinates, composer keystrokes, database access, or destructive menu actions.
property maxDepth : 16

on normalized(s)
    if s is missing value then return ""
    set t to current application's NSString's stringWithString:(s as text)
    repeat with codepoint in {8206, 8207, 8294, 8295, 8296, 8297}
        set t to t's stringByReplacingOccurrencesOfString:(character id codepoint) withString:""
    end repeat
    return (t's stringByTrimmingCharactersInSet:(current application's NSCharacterSet's whitespaceAndNewlineCharacterSet())) as text
end normalized

on jsonText(valueObject)
    set d to current application's NSJSONSerialization's dataWithJSONObject:valueObject options:0 |error|:(missing value)
    return (current application's NSString's alloc()'s initWithData:d encoding:4) as text
end jsonText

on isExportConfirmation(messageText, buttonTitle, buttonIdentifier)
    return normalized(messageText) is "Exported chat was saved to Downloads Folder" and normalized(buttonTitle) is "OK" and buttonIdentifier is "action-button--999"
end isExportConfirmation

on isCenterVisible(rowPosition, rowSize, viewportPosition, viewportSize)
    if item 1 of rowSize <= 0 or item 2 of rowSize <= 0 then return false
    set centerX to (item 1 of rowPosition) + (item 1 of rowSize) / 2
    set centerY to (item 2 of rowPosition) + (item 2 of rowSize) / 2
    return centerX >= (item 1 of viewportPosition) and centerX < (item 1 of viewportPosition) + (item 1 of viewportSize) and centerY >= (item 2 of viewportPosition) and centerY < (item 2 of viewportPosition) + (item 2 of viewportSize)
end isCenterVisible

on chatViewport(listPosition, listSize, toolbarPosition, toolbarSize)
    -- UIKit reports the list's frame behind its overlaid search/navigation toolbar.
    set bottomEdge to (item 2 of listPosition) + (item 2 of listSize)
    set topEdge to (item 2 of toolbarPosition) + (item 2 of toolbarSize)
    if topEdge < (item 2 of listPosition) or topEdge >= bottomEdge then error "inventory-incomplete"
    return {{item 1 of listPosition, topEdge}, {item 1 of listSize, bottomEdge - topEdge}}
end chatViewport

on visibleChatRows(listNode)
    set visibleRows to {}
    set toolbarNode to findID(appWindow(), "Toolbar", 0)
    if toolbarNode is missing value then error "inventory-incomplete"
    tell application "/System/Library/CoreServices/System Events.app"
        set viewport to my chatViewport(position of listNode, size of listNode, position of toolbarNode, size of toolbarNode)
        set viewportPosition to item 1 of viewport
        set viewportSize to item 2 of viewport
        repeat with rowRef in (buttons of listNode)
            if my isCenterVisible(position of rowRef, size of rowRef, viewportPosition, viewportSize) then set end of visibleRows to contents of rowRef
        end repeat
    end tell
    return visibleRows
end visibleChatRows

on findID(rootNode, identifier, depth)
    set pendingNodes to {rootNode}
    set nodeIndex to 1
    tell application "/System/Library/CoreServices/System Events.app"
        repeat while nodeIndex <= (count pendingNodes)
            if nodeIndex > 2000 then error "accessibility-tree-limit"
            set thisNode to item nodeIndex of pendingNodes
            set nodeIndex to nodeIndex + 1
            set nodeIdentifier to ""
            try
                set nodeIdentifier to value of attribute "AXIdentifier" of thisNode
                if nodeIdentifier is identifier then return thisNode
            end try
            if nodeIdentifier is not in {"ChatListView_TableView", "WAArchivedChatsViewController"} then
                set pendingNodes to pendingNodes & (UI elements of thisNode)
            end if
        end repeat
    end tell
    return missing value
end findID

on findControl(rootNode, labelText, roleName, depth)
    set pendingNodes to {rootNode}
    set nodeIndex to 1
    tell application "/System/Library/CoreServices/System Events.app"
        repeat while nodeIndex <= (count pendingNodes)
            if nodeIndex > 2000 then error "accessibility-tree-limit"
            set thisNode to item nodeIndex of pendingNodes
            set nodeIndex to nodeIndex + 1
            set nodeIdentifier to ""
            try
                set nodeIdentifier to value of attribute "AXIdentifier" of thisNode
            end try
            if role of thisNode is roleName then
                if my normalized(description of thisNode) is labelText and nodeIdentifier is not "MediaHub" then return thisNode
            end if
            if nodeIdentifier is not in {"ChatListView_TableView", "WAArchivedChatsViewController"} then
                set pendingNodes to pendingNodes & (UI elements of thisNode)
            end if
        end repeat
    end tell
    return missing value
end findControl

on appWindow()
    tell application "/System/Library/CoreServices/System Events.app"
        if not (exists process "WhatsApp") then error "app-disconnected"
        tell process "WhatsApp"
            if (count windows) is not 1 then error "session-unavailable"
            return window 1
        end tell
    end tell
end appWindow

on waitForMainWindow()
    -- Dismissing a sheet can briefly leave the accessibility tree unavailable.
    repeat 30 times
        try
            set w to appWindow()
            tell application "/System/Library/CoreServices/System Events.app" to set hasSheet to (count sheets of w) > 0
            if not hasSheet then
                if findID(w, "Sidebar_view", 0) is not missing value then return w
            end if
        end try
        delay 0.2
    end repeat
    error "export-control-changed"
end waitForMainWindow

on verifyProfile(expected)
    tell application "/System/Library/CoreServices/System Events.app" to tell process "WhatsApp"
        set frontmost to true
    end tell
    set existingProfile to findID(appWindow(), "SettingsView_ProfileCell", 0)
    if existingProfile is missing value then
        waitForMainWindow()
        set settingsNode to findControl(appWindow(), "Settings", "AXButton", 0)
        if settingsNode is missing value then error "export-control-changed"
        tell application "/System/Library/CoreServices/System Events.app" to click settingsNode
    end if
    set profileNode to findID(appWindow(), "ProfileView_UsernameCell", 0)
    if profileNode is missing value then error "identity-unverified"
    tell application "/System/Library/CoreServices/System Events.app"
        set labels to static texts of profileNode
        if (count labels) is not 1 then error "identity-unverified"
        set observed to my normalized(description of item 1 of labels)
    end tell
    if observed is not expected then error "identity-unverified"
    set doneNode to findControl(appWindow(), "Done", "AXButton", 0)
    if doneNode is missing value then error "export-control-changed"
    tell application "/System/Library/CoreServices/System Events.app" to click doneNode
    waitForMainWindow()
    return observed
end verifyProfile

on selectList(listName)
    if listName is "main" then
        set labelText to "Chats"
    else if listName is "archived" then
        set labelText to "Archived"
    else
        error "invalid-list"
    end if
    set nav to findID(appWindow(), "Sidebar_view", 0)
    if nav is missing value then error "export-control-changed"
    set buttonNode to findControl(nav, labelText, "AXButton", 0)
    if buttonNode is missing value then error "export-control-changed"
    tell application "/System/Library/CoreServices/System Events.app" to click buttonNode
    set searchNode to findID(appWindow(), "TokenizedSearchBar_TextView", 0)
    if searchNode is not missing value then
        tell application "/System/Library/CoreServices/System Events.app"
            set searchValue to my normalized(value of searchNode)
        end tell
        if searchValue is not in {"", "Search"} then error "filtered-inventory"
    end if
    if listName is "main" then
        set listIdentifier to "ChatListView_TableView"
    else
        set listIdentifier to "WAArchivedChatsViewController"
    end if
    set listNode to findID(appWindow(), listIdentifier, 0)
    if listNode is missing value then error "inventory-incomplete"
    return listNode
end selectList

on rowTitle(rowNode)
    set titleNode to findID(rowNode, "ChatSessionCell_Name", 0)
    if titleNode is missing value then error "unidentified-chat-row"
    tell application "/System/Library/CoreServices/System Events.app" to return my normalized(description of titleNode)
end rowTitle

on listTitles(listNode)
    set resultTitles to {}
    -- Passive scans may read prefetched cells for overlap, but never interact with them.
    tell application "/System/Library/CoreServices/System Events.app" to set chatRows to buttons of listNode
    repeat with rowRef in chatRows
        set end of resultTitles to rowTitle(contents of rowRef)
    end repeat
    return resultTitles
end listTitles

on chatKind(rowNode)
    tell application "/System/Library/CoreServices/System Events.app" to perform action "AXShowMenu" of rowNode
    tell application "/System/Library/CoreServices/System Events.app" to tell process "WhatsApp"
        repeat 30 times
            if exists menu 1 of group 1 of window 1 then exit repeat
            delay 0.1
        end repeat
        if not (exists menu 1 of group 1 of window 1) then error "export-control-changed"
        set observedKind to "unknown"
        repeat with menuChoice in (menu items of menu 1 of group 1 of window 1)
            set choiceTitle to my normalized(name of menuChoice)
            if choiceTitle is "Contact info" then set observedKind to "direct"
            if choiceTitle is "Group info" then set observedKind to "group"
        end repeat
        perform action "AXCancel" of menu 1 of group 1 of window 1
        repeat 30 times
            if not (exists menu 1 of group 1 of window 1) then return observedKind
            delay 0.1
        end repeat
    end tell
    error "export-control-changed"
end chatKind

on shouldInspectRow(observedTitle, targetTitle, isVisible)
    return targetTitle is not "" and observedTitle is targetTitle and isVisible
end shouldInspectRow

on listEntries(listNode, targetTitle)
    set originalTitles to listTitles(listNode)
    set resultEntries to {}
    tell application "/System/Library/CoreServices/System Events.app" to set chatRows to buttons of listNode
    if targetTitle is not "" then
        set toolbarNode to findID(appWindow(), "Toolbar", 0)
        if toolbarNode is missing value then error "inventory-incomplete"
        tell application "/System/Library/CoreServices/System Events.app" to set viewport to my chatViewport(position of listNode, size of listNode, position of toolbarNode, size of toolbarNode)
    end if
    repeat with rowRef in chatRows
        set observedTitle to rowTitle(contents of rowRef)
        set observedKind to "unknown"
        set rowVisible to false
        if targetTitle is not "" and observedTitle is targetTitle then
            tell application "/System/Library/CoreServices/System Events.app" to set rowVisible to my isCenterVisible(position of rowRef, size of rowRef, item 1 of viewport, item 2 of viewport)
        end if
        if shouldInspectRow(observedTitle, targetTitle, rowVisible) then
            set observedKind to chatKind(contents of rowRef)
        end if
        set end of resultEntries to {|title|:observedTitle, |kind|:observedKind}
    end repeat
    if listTitles(listNode) is not originalTitles then error "inventory-changed-during-scan"
    return resultEntries
end listEntries

on inventoryList(listName, targetTitle)
    set listNode to selectList(listName)
    set previousTitles to listTitles(listNode)
    set topReached to false
    set stableTop to 0
    repeat 200 times
        tell application "/System/Library/CoreServices/System Events.app"
            if not (exists action "AXScrollUpByPage" of listNode) then
                set topReached to true
                exit repeat
            end if
        end tell
        tell application "/System/Library/CoreServices/System Events.app" to perform action "AXScrollUpByPage" of listNode
        delay 0.3
        set currentTitles to listTitles(listNode)
        if currentTitles is previousTitles then
            set stableTop to stableTop + 1
        else
            set stableTop to 0
        end if
        if stableTop >= 3 then
            set topReached to true
            exit repeat
        end if
        set previousTitles to currentTitles
    end repeat
    if not topReached then error "inventory-incomplete"
    set currentTitles to listTitles(listNode)
    set collectedPages to {listEntries(listNode, targetTitle)}
    set testedRoundTrip to false
    repeat 500 times
        tell application "/System/Library/CoreServices/System Events.app"
            if not (exists action "AXScrollDownByPage" of listNode) then return {|pages|:collectedPages, |topReached|:true, |bottomReached|:true}
        end tell
        tell application "/System/Library/CoreServices/System Events.app" to perform action "AXScrollDownByPage" of listNode
        delay 0.3
        set nextTitles to listTitles(listNode)
        if nextTitles is currentTitles then error "inventory-scroll-stalled"
        if not testedRoundTrip then
            tell application "/System/Library/CoreServices/System Events.app" to perform action "AXScrollUpByPage" of listNode
            delay 0.3
            if listTitles(listNode) is not currentTitles then error "inventory-scroll-stalled"
            tell application "/System/Library/CoreServices/System Events.app" to perform action "AXScrollDownByPage" of listNode
            delay 0.3
            if listTitles(listNode) is not nextTitles then error "inventory-changed-during-scan"
            set testedRoundTrip to true
        end if
        if nextTitles is not currentTitles then set end of collectedPages to listEntries(listNode, targetTitle)
        set currentTitles to nextTitles
    end repeat
    error "inventory-incomplete"
end inventoryList

on exportChat(listName, expectedTitle, expectedKind)
    set listNode to selectList(listName)
    set matches to {}
    repeat with passIndex from 1 to 2
        repeat 500 times
            set chatRows to visibleChatRows(listNode)
            repeat with rowRef in chatRows
                if rowTitle(contents of rowRef) is expectedTitle then
                    if expectedKind is "unknown" then
                        set end of matches to contents of rowRef
                    else if chatKind(contents of rowRef) is expectedKind then
                        set end of matches to contents of rowRef
                    end if
                end if
            end repeat
            if (count matches) > 0 then exit repeat
            tell application "/System/Library/CoreServices/System Events.app"
                if not (exists action "AXScrollDownByPage" of listNode) then exit repeat
                perform action "AXScrollDownByPage" of listNode
            end tell
            delay 0.3
        end repeat
        if (count matches) > 0 then exit repeat
        set stableTop to 0
        set previousTitles to listTitles(listNode)
        repeat 200 times
            tell application "/System/Library/CoreServices/System Events.app"
                if not (exists action "AXScrollUpByPage" of listNode) then exit repeat
                perform action "AXScrollUpByPage" of listNode
            end tell
            delay 0.3
            set currentTitles to listTitles(listNode)
            if currentTitles is previousTitles then
                set stableTop to stableTop + 1
            else
                set stableTop to 0
            end if
            if stableTop >= 3 then exit repeat
            set previousTitles to currentTitles
        end repeat
    end repeat
    if (count matches) is not 1 then error "identity-unverified"
    set observedKind to chatKind(item 1 of matches)
    if expectedKind is not "unknown" and observedKind is not expectedKind then error "identity-unverified"
    tell application "/System/Library/CoreServices/System Events.app" to perform action "AXShowMenu" of item 1 of matches
    tell application "/System/Library/CoreServices/System Events.app" to tell process "WhatsApp"
        repeat 30 times
            if exists menu 1 of group 1 of window 1 then exit repeat
            delay 0.2
        end repeat
        if not (exists menu 1 of group 1 of window 1) then error "export-control-changed"
        set exportItems to {}
        repeat with choiceRef in (menu items of menu 1 of group 1 of window 1)
            if my normalized(name of choiceRef) is "Export chat" then set end of exportItems to contents of choiceRef
        end repeat
        set exportAvailable to (count exportItems) is 1
        if exportAvailable then set exportAvailable to enabled of item 1 of exportItems
        if not exportAvailable then
            -- Only cancel the verified chat menu, never send Escape into a composer.
            if exists action "AXCancel" of menu 1 of group 1 of window 1 then
                perform action "AXCancel" of menu 1 of group 1 of window 1
                error "export-unavailable"
            end if
            error "export-control-changed"
        end if
        click item 1 of exportItems
    end tell
    set mediaRequested to false
    repeat 240 times
        tell application "/System/Library/CoreServices/System Events.app" to tell process "WhatsApp"
            if exists sheet 1 of window 1 then
                set exportSheet to sheet 1 of window 1
                set doneButtons to buttons of exportSheet
                if (count doneButtons) is not 1 then error "export-control-changed"
                set doneButton to item 1 of doneButtons
                set confirmed to false
                repeat with labelRef in (static texts of exportSheet)
                    if my isExportConfirmation(name of labelRef, name of doneButton, value of attribute "AXIdentifier" of doneButton) then set confirmed to true
                end repeat
                if not confirmed then error "export-control-changed"
                click item 1 of doneButtons
                my waitForMainWindow()
                return {|operation|:"export", |status|:"ok", |title|:expectedTitle, |kind|:observedKind, |mediaRequested|:mediaRequested, |downloadObserved|:true}
            end if
        end tell
        if not mediaRequested then
            -- The success sheet can replace the tree while the media choice is being located.
            set mediaNode to missing value
            try
                set mediaNode to findControl(appWindow(), "Attach media", "AXButton", 0)
            end try
            if mediaNode is not missing value then
                tell application "/System/Library/CoreServices/System Events.app" to click mediaNode
                set mediaRequested to true
            end if
        end if
        delay 0.25
    end repeat
    error "export-not-delivered"
end exportChat

on run argv
    if (count argv) < 2 then error "unsupported-operation"
    set operationName to item 1 of argv
    if operationName is not in {"inventory", "resolve", "export"} then error "unsupported-operation"
    if operationName is "export" and ((count argv) is not in {4, 5}) then error "invalid-export-arguments"
    if operationName is "inventory" and ((count argv) is not 2) then error "invalid-inventory-arguments"
    if operationName is "resolve" and ((count argv) is not 3) then error "invalid-resolve-arguments"
    set expectedKind to "unknown"
    if (count argv) is 5 then set expectedKind to item 5 of argv
    if expectedKind is not in {"unknown", "direct", "group"} then error "invalid-export-arguments"
    set observed to verifyProfile(item 2 of argv)
    if operationName is in {"inventory", "resolve"} then
        set targetTitle to ""
        if operationName is "resolve" then set targetTitle to item 3 of argv
        set mainTitles to inventoryList("main", targetTitle)
        set archivedTitles to inventoryList("archived", targetTitle)
        selectList("main")
        return jsonText({|operation|:operationName, |status|:"ok", |profile|:observed, |pages|:{|main|:mainTitles, |archived|:archivedTitles}})
    end if
    return jsonText(exportChat(item 3 of argv, item 4 of argv, expectedKind))
end run
