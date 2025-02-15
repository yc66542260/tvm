#!/bin/bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.


set -euo pipefail

SRC_BASE=$PWD

cd ../csi-nn2/

#make nn2_openvx
make nn2_ref_x86
#make nn2_pnna_x86
#make nn2_pnna
#make nn2_c906
#make nn2_c908
#make nn2_hlight
#make nn2_hlight_x86
#make nn2_asp_elf
make install_nn2

cd -

cp ../csi-nn2/install_nn2 . -r

echo "CSI install dir: " "$SRC_BASE/install_nn2"
