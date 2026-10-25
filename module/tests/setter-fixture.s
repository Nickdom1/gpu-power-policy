# Synthetic ABI fixture, not NVIDIA code. No device access.
.section .text
.org 0x7ca70-0x10000,0x90
.global nvmlDeviceSetPowerManagementLimit
.type nvmlDeviceSetPowerManagementLimit,@function
nvmlDeviceSetPowerManagementLimit:
 push %rbp
 mov %rsp,%rbp
 mov %esi,%r8d
 mov %rdi,%rsi
 xor %edx,%edx
 xor %ecx,%ecx
 call fixture_hal
 leave
 ret
.global nvmlDeviceSetPowerManagementLimit_v2
.type nvmlDeviceSetPowerManagementLimit_v2,@function
nvmlDeviceSetPowerManagementLimit_v2:
 ud2
.org 0xf5b00-0x10000,0x90
fixture_hal:
 push %rbp
 mov %rsp,%rbp
 sub $16,%rsp
 mov fixture_packet(%rip),%rax
 mov %rax,(%rsp)
 mov fixture_packet+8(%rip),%eax
 mov %eax,8(%rsp)
 mov fixture_command(%rip),%ecx
 mov fixture_size(%rip),%r9d
 mov %rsp,%r8
 jmp fixture_request
.org 0xf5be7-0x10000,0x90
fixture_request:
 call fixture_rm
 nop
 leave
 ret
fixture_rm:
 incl fixture_count(%rip)
 mov (%r8),%rax
 mov %rax,fixture_after(%rip)
 mov 8(%r8),%eax
 mov %eax,fixture_after+8(%rip)
 mov fixture_status(%rip),%eax
 ret
.section .data
.global fixture_packet,fixture_after,fixture_command,fixture_size,fixture_count,fixture_status
.protected fixture_packet,fixture_after,fixture_command,fixture_size,fixture_count,fixture_status
fixture_packet: .byte 1,254,0,0,72,232,1,0,0,0,0,0
fixture_after: .zero 12
fixture_command: .long 0x2080e61e
fixture_size: .long 12
fixture_count: .long 0
fixture_status: .long 0
.section .note.GNU-stack,"",@progbits
