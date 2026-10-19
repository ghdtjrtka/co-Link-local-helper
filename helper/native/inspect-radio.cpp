#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <winioctl.h>
#include <initguid.h>
#include <bthdef.h>
#include <bthioctl.h>
#include <setupapi.h>
#include <devguid.h>
#include <iostream>
#include <string>
#include <vector>

// Read-only lookup for the selected Windows Bluetooth stack interface. No USB handles or writes.
int wmain(int argc, wchar_t** argv) {
    if (argc != 2) return 2;
    HDEVINFO set = SetupDiGetClassDevs(&GUID_BTHPORT_DEVICE_INTERFACE, nullptr, nullptr,
                                      DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    if (set == INVALID_HANDLE_VALUE) return 3;
    int result = 4;
    for (DWORD i=0;;++i) {
        SP_DEVICE_INTERFACE_DATA iface{}; iface.cbSize=sizeof(iface);
        if (!SetupDiEnumDeviceInterfaces(set,nullptr,&GUID_BTHPORT_DEVICE_INTERFACE,i,&iface)) break;
        DWORD need=0; SetupDiGetDeviceInterfaceDetail(set,&iface,nullptr,0,&need,nullptr);
        std::vector<BYTE> mem(need); auto detail=reinterpret_cast<SP_DEVICE_INTERFACE_DETAIL_DATA*>(mem.data());
        detail->cbSize=sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA);
        SP_DEVINFO_DATA dev{}; dev.cbSize=sizeof(dev);
        if (!SetupDiGetDeviceInterfaceDetail(set,&iface,detail,need,nullptr,&dev)) continue;
        wchar_t id[4096]{};
        if (!SetupDiGetDeviceInstanceId(set,&dev,id,4096,nullptr) || _wcsicmp(id,argv[1])) continue;
        HANDLE h=CreateFile(detail->DevicePath,GENERIC_READ,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr);
        if (h==INVALID_HANDLE_VALUE) { result=5; break; }
        BTH_LOCAL_RADIO_INFO info{}; DWORD returned=0;
        BOOL ok=DeviceIoControl(h,IOCTL_BTH_GET_LOCAL_INFO,nullptr,0,&info,sizeof(info),&returned,nullptr);
        CloseHandle(h);
        if (ok && returned>=sizeof(info)) {
            std::wcout << L"{\"controller_address\":\"";
            for(int b=5;b>=0;--b) { if(b!=5) std::wcout<<L":"; std::wcout.width(2); std::wcout.fill(L'0'); std::wcout<<std::hex<<(unsigned)((info.localInfo.address >> (b*8)) & 0xff); }
            std::wcout << L"\",\"version\":" << std::dec << (unsigned)info.radioInfo.lmpVersion << L"}";
            result=0;
        } else result=6;
        break;
    }
    SetupDiDestroyDeviceInfoList(set); return result;
}
