cmake_minimum_required(VERSION 3.16)

# Firmware version — change this before building for OTA!
set(PROJECT_VER "3.0.0")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(three_body_ota)