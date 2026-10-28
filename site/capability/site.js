'use strict';
const chooser=document.getElementById('example-select');
if(chooser){chooser.addEventListener('change',()=>{document.querySelectorAll('.example').forEach((panel,index)=>{panel.hidden=String(index)!==chooser.value;});});}
