#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0600
#include <windows.h>

#include <stdbool.h>
#include <stdio.h>
#include <wchar.h>

#define COMMAND_TIMEOUT_MS 5000
#define TEXT_CAPACITY 256

typedef struct {
  HWND window;
} WindowSearch;

typedef struct {
  unsigned int count;
} ControlEnumeration;

typedef struct {
  int identifier;
  HWND control;
} ControlSearch;

typedef struct {
  UINT identifier;
  wchar_t text[TEXT_CAPACITY];
  bool found;
} MenuCommand;

typedef struct {
  const wchar_t *wanted;
  MenuCommand command;
  HMENU resource_menu;
} MenuResourceSearch;

static void normalize_text(const wchar_t *source, wchar_t *destination,
                           size_t capacity) {
  size_t output = 0;
  for (size_t input = 0; source[input] != L'\0' && output + 1 < capacity;
       ++input) {
    if (source[input] == L'&') {
      continue;
    }
    if (source[input] == L'\t') {
      break;
    }
    destination[output++] = source[input];
  }
  while (output > 0 && destination[output - 1] == L' ') {
    --output;
  }
  destination[output] = L'\0';
}

static BOOL CALLBACK find_power_center_window(HWND window, LPARAM parameter) {
  WindowSearch *search = (WindowSearch *)parameter;
  wchar_t title[TEXT_CAPACITY] = {0};
  wchar_t class_name[TEXT_CAPACITY] = {0};
  if (!IsWindowVisible(window) || GetWindowTextW(window, title, TEXT_CAPACITY) == 0 ||
      GetClassNameW(window, class_name, TEXT_CAPACITY) == 0) {
    return TRUE;
  }
  if (wcsstr(title, L"Power Center") != NULL &&
      wcsncmp(class_name, L"Afx:", 4) == 0) {
    search->window = window;
    return FALSE;
  }
  return TRUE;
}

static HWND require_power_center_window(void) {
  WindowSearch search = {0};
  EnumWindows(find_power_center_window, (LPARAM)&search);
  if (search.window == NULL) {
    fwprintf(stderr,
             L"error: could not find a visible Pwrcntr.exe Power Center window\n");
  }
  return search.window;
}

static BOOL CALLBACK print_child_control(HWND control, LPARAM parameter) {
  ControlEnumeration *enumeration = (ControlEnumeration *)parameter;
  wchar_t text[TEXT_CAPACITY] = {0};
  wchar_t class_name[TEXT_CAPACITY] = {0};
  GetWindowTextW(control, text, TEXT_CAPACITY);
  GetClassNameW(control, class_name, TEXT_CAPACITY);
  LONG_PTR identifier = GetWindowLongPtrW(control, GWLP_ID);
  LONG_PTR style = GetWindowLongPtrW(control, GWL_STYLE);
  LRESULT check = SendMessageW(control, BM_GETCHECK, 0, 0);
  RECT rectangle = {0};
  GetWindowRect(control, &rectangle);
  POINT origin = {.x = rectangle.left, .y = rectangle.top};
  ScreenToClient(GetParent(control), &origin);
  wprintf(L"control: hwnd=0x%p id=0x%04lx class=%ls style=0x%08lx "
          L"check=%ld x=%ld y=%ld w=%ld h=%ld text=%ls\n",
          (void *)control, (unsigned long)identifier, class_name,
          (unsigned long)style, (long)check, origin.x, origin.y,
          rectangle.right - rectangle.left, rectangle.bottom - rectangle.top,
          text[0] == L'\0' ? L"<empty>" : text);
  enumeration->count++;
  return TRUE;
}

static BOOL CALLBACK find_control_by_id(HWND control, LPARAM parameter) {
  ControlSearch *search = (ControlSearch *)parameter;
  if (GetDlgCtrlID(control) == search->identifier) {
    search->control = control;
    return FALSE;
  }
  return TRUE;
}

static HWND require_control(HWND window, int identifier) {
  ControlSearch search = {.identifier = identifier, .control = NULL};
  EnumChildWindows(window, find_control_by_id, (LPARAM)&search);
  return search.control;
}

enum {
  CONTROL_MODE_KEY = 0x03e8,
  CONTROL_MODE_AUTO = 0x03e9,
  CONTROL_MODE_TIMEOUT = 0x03ea,
  CONTROL_MODE_SERVICE = 0x03eb,
};

static int select_mode(HWND window, int wanted_identifier,
                       const wchar_t *wanted_name) {
  for (unsigned int attempt = 0; attempt < 6; attempt++) {
    // Pwrcntr rebuilds this child dialog after a mode change, invalidating
    // every previously discovered HWND. Resolve both controls on each pass.
    HWND mode_key = require_control(window, CONTROL_MODE_KEY);
    HWND wanted_indicator = require_control(window, wanted_identifier);
    if (mode_key == NULL || wanted_indicator == NULL) {
      fwprintf(stderr, L"error: Power Center mode controls were not found\n");
      return 11;
    }
    if (SendMessageW(wanted_indicator, BM_GETCHECK, 0, 0) == BST_CHECKED) {
      wprintf(L"selected mode: %ls\n", wanted_name);
      return 0;
    }
    DWORD_PTR response = 0;
    if (!SendMessageTimeoutW(mode_key, BM_CLICK, 0, 0,
                             SMTO_ABORTIFHUNG | SMTO_BLOCK,
                             COMMAND_TIMEOUT_MS, &response)) {
      fwprintf(stderr, L"error: Power Center mode key could not be clicked "
                       L"(%lu)\n",
               GetLastError());
      return 12;
    }
    // The mode indicator is recreated asynchronously after the click. Poll
    // the replacement control before sending another click; otherwise a slow
    // redraw can make several queued clicks skip over the requested mode.
    for (unsigned int poll = 0; poll < 20; poll++) {
      Sleep(50);
      wanted_indicator = require_control(window, wanted_identifier);
      if (wanted_indicator != NULL &&
          SendMessageW(wanted_indicator, BM_GETCHECK, 0, 0) == BST_CHECKED) {
        wprintf(L"selected mode: %ls\n", wanted_name);
        return 0;
      }
    }
  }
  HWND wanted_indicator = require_control(window, wanted_identifier);
  if (wanted_indicator != NULL &&
      SendMessageW(wanted_indicator, BM_GETCHECK, 0, 0) == BST_CHECKED) {
    wprintf(L"selected mode: %ls\n", wanted_name);
    return 0;
  }
  fwprintf(stderr, L"error: Power Center did not enter %ls mode\n", wanted_name);
  return 13;
}

static HMODULE load_power_center_resources(HWND window) {
  DWORD process_id = 0;
  GetWindowThreadProcessId(window, &process_id);
  HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                               process_id);
  if (process == NULL) {
    fwprintf(stderr, L"error: could not open Power Center process %lu (%lu)\n",
             process_id, GetLastError());
    return NULL;
  }
  wchar_t path[MAX_PATH] = {0};
  DWORD path_length = MAX_PATH;
  BOOL found = QueryFullProcessImageNameW(process, 0, path, &path_length);
  CloseHandle(process);
  if (!found) {
    fwprintf(stderr, L"error: could not determine Pwrcntr.exe path (%lu)\n",
             GetLastError());
    return NULL;
  }
  HMODULE module = LoadLibraryExW(
      path, NULL, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE);
  if (module == NULL) {
    fwprintf(stderr, L"error: could not load menu resources from %ls (%lu)\n",
             path, GetLastError());
  }
  return module;
}

static bool menu_text(HMENU menu, int position, wchar_t *text,
                      size_t capacity) {
  return GetMenuStringW(menu, (UINT)position, text, (int)capacity,
                        MF_BYPOSITION) > 0;
}

static bool find_menu_command(HMENU menu, const wchar_t *wanted,
                              MenuCommand *result) {
  int count = GetMenuItemCount(menu);
  for (int position = 0; position < count; ++position) {
    wchar_t raw[TEXT_CAPACITY] = {0};
    wchar_t normalized[TEXT_CAPACITY] = {0};
    if (!menu_text(menu, position, raw, TEXT_CAPACITY)) {
      continue;
    }
    normalize_text(raw, normalized, TEXT_CAPACITY);
    HMENU child = GetSubMenu(menu, position);
    if (child != NULL && find_menu_command(child, wanted, result)) {
      return true;
    }
    if (child == NULL && _wcsicmp(normalized, wanted) == 0) {
      UINT identifier = GetMenuItemID(menu, position);
      if (identifier == 0 || identifier == (UINT)-1) {
        continue;
      }
      result->identifier = identifier;
      wcsncpy(result->text, normalized, TEXT_CAPACITY - 1);
      result->text[TEXT_CAPACITY - 1] = L'\0';
      result->found = true;
      return true;
    }
  }
  return false;
}

static BOOL CALLBACK search_menu_resource(HMODULE module, LPCWSTR type,
                                          LPWSTR name, LONG_PTR parameter) {
  (void)type;
  MenuResourceSearch *search = (MenuResourceSearch *)parameter;
  HMENU menu = LoadMenuW(module, name);
  if (menu == NULL) {
    return TRUE;
  }
  bool found = find_menu_command(menu, search->wanted, &search->command);
  if (found) {
    search->resource_menu = menu;
  } else {
    DestroyMenu(menu);
  }
  return found ? FALSE : TRUE;
}

static int send_menu_command(HWND window, const wchar_t *wanted) {
  HMODULE module = load_power_center_resources(window);
  if (module == NULL) {
    return 4;
  }
  MenuResourceSearch search = {.wanted = wanted};
  EnumResourceNamesW(module, RT_MENU, search_menu_resource,
                     (LONG_PTR)&search);
  FreeLibrary(module);
  if (!search.command.found) {
    fwprintf(stderr, L"error: menu command '%ls' was not found\n", wanted);
    return 5;
  }
  if (!PostMessageW(window, WM_COMMAND,
                    MAKEWPARAM(search.command.identifier, 0), 0)) {
      fwprintf(stderr, L"error: menu command '%ls' could not be queued (%lu)\n",
               search.command.text, GetLastError());
      DestroyMenu(search.resource_menu);
      return 6;
  }
  DWORD_PTR response = 0;
  SendMessageTimeoutW(window, WM_NULL, 0, 0,
                      SMTO_ABORTIFHUNG | SMTO_BLOCK,
                      COMMAND_TIMEOUT_MS, &response);
  DestroyMenu(search.resource_menu);
  wprintf(L"selected: %ls (command 0x%04x)\n", search.command.text,
          search.command.identifier);
  return 0;
}

static void print_menu(HMENU menu, unsigned int depth) {
  int count = GetMenuItemCount(menu);
  for (int position = 0; position < count; ++position) {
    wchar_t raw[TEXT_CAPACITY] = {0};
    wchar_t normalized[TEXT_CAPACITY] = {0};
    if (!menu_text(menu, position, raw, TEXT_CAPACITY)) {
      continue;
    }
    normalize_text(raw, normalized, TEXT_CAPACITY);
    HMENU child = GetSubMenu(menu, position);
    UINT identifier = GetMenuItemID(menu, position);
    MENUITEMINFOW item = {0};
    item.cbSize = sizeof(item);
    item.fMask = MIIM_STATE;
    GetMenuItemInfoW(menu, (UINT)position, TRUE, &item);
    wprintf(L"%*ls%ls%ls", (int)(depth * 2), L"",
            child == NULL ? L"- " : L"+ ",
            normalized[0] == L'\0' ? L"<separator>" : normalized);
    if (child == NULL && identifier != 0 && identifier != (UINT)-1) {
      wprintf(L" [0x%04x]", identifier);
    }
    if ((item.fState & MFS_CHECKED) != 0) {
      wprintf(L" [checked]");
    }
    if ((item.fState & (MFS_DISABLED | MFS_GRAYED)) != 0) {
      wprintf(L" [disabled]");
    }
    wprintf(L"\n");
    if (child != NULL) {
      print_menu(child, depth + 1);
    }
  }
}

static BOOL CALLBACK print_menu_resource(HMODULE module, LPCWSTR type,
                                         LPWSTR name, LONG_PTR parameter) {
  (void)type;
  (void)parameter;
  HMENU menu = LoadMenuW(module, name);
  if (menu == NULL) {
    return TRUE;
  }
  if (IS_INTRESOURCE(name)) {
    wprintf(L"menu resource: %u\n", (unsigned int)(ULONG_PTR)name);
  } else {
    wprintf(L"menu resource: %ls\n", name);
  }
  print_menu(menu, 0);
  DestroyMenu(menu);
  return TRUE;
}

static void usage(void) {
  fwprintf(stderr,
           L"usage:\n"
           L"  pwrcntr-control.exe status\n"
           L"  pwrcntr-control.exe list\n"
           L"  pwrcntr-control.exe controls\n"
           L"  pwrcntr-control.exe model \"E260808 (PD 8 Combo)\"\n"
           L"  pwrcntr-control.exe port COM3\n"
           L"  pwrcntr-control.exe mode auto|service|timeout\n"
           L"  pwrcntr-control.exe power toggle\n");
}

int wmain(int argument_count, wchar_t **arguments) {
  if (argument_count < 2) {
    usage();
    return 2;
  }
  HWND window = require_power_center_window();
  if (window == NULL) {
    return 3;
  }
  if (_wcsicmp(arguments[1], L"status") == 0) {
    wchar_t title[TEXT_CAPACITY] = {0};
    DWORD process_id = 0;
    GetWindowTextW(window, title, TEXT_CAPACITY);
    GetWindowThreadProcessId(window, &process_id);
    wprintf(L"running: pid=%lu hwnd=0x%p title=%ls\n", process_id,
            (void *)window, title);
    return 0;
  }
  if (_wcsicmp(arguments[1], L"list") == 0) {
    HMODULE module = load_power_center_resources(window);
    if (module == NULL) {
      return 4;
    }
    if (!EnumResourceNamesW(module, RT_MENU, print_menu_resource, 0)) {
      fwprintf(stderr, L"error: could not enumerate Power Center menus (%lu)\n",
               GetLastError());
      FreeLibrary(module);
      return 7;
    }
    FreeLibrary(module);
    return 0;
  }
  if (_wcsicmp(arguments[1], L"controls") == 0) {
    ControlEnumeration enumeration = {0};
    EnumChildWindows(window, print_child_control, (LPARAM)&enumeration);
    wprintf(L"controls: %u\n", enumeration.count);
    return 0;
  }
  if (_wcsicmp(arguments[1], L"model") == 0 && argument_count == 3) {
    return send_menu_command(window, arguments[2]);
  }
  if (_wcsicmp(arguments[1], L"port") == 0 && argument_count == 3) {
    return send_menu_command(window, arguments[2]);
  }
  if (_wcsicmp(arguments[1], L"mode") == 0 && argument_count == 3) {
    if (_wcsicmp(arguments[2], L"auto") == 0) {
      return select_mode(window, CONTROL_MODE_AUTO, L"Auto");
    }
    if (_wcsicmp(arguments[2], L"service") == 0) {
      return select_mode(window, CONTROL_MODE_SERVICE, L"Service");
    }
    if (_wcsicmp(arguments[2], L"timeout") == 0) {
      return select_mode(window, CONTROL_MODE_TIMEOUT, L"Time-Out");
    }
  }
  if (_wcsicmp(arguments[1], L"power") == 0 && argument_count == 3 &&
      _wcsicmp(arguments[2], L"toggle") == 0) {
    return send_menu_command(window, L"Switch Power");
  }
  usage();
  return 2;
}
