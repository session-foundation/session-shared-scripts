. venv/bin/activate

rm -rf /tmp/strings_download_directory /tmp/strings_output_directory
python crowdin/download_translations_from_crowdin.py db687d6437eb61196bdb5cf18d6b2a9c6f8bea968f2da097b9c3044198377e26ed930c4f4b31184d 618696 --glossary_id 407522 --concept_id 36 /tmp/strings_download_directory --skip-untranslated-strings

python "crowdin/generate_desktop_strings.py" "/tmp/strings_download_directory" "/tmp/strings_output_directory/_locales" "/tmp/strings_output_directory/desktop/ts/localization/constants.ts" && nautilus /tmp/strings_output_directory/_locales /tmp/strings_output_directory/desktop/ts/localization/


python "crowdin/generate_android_strings.py" "/tmp/strings_download_directory" "/tmp/strings_output_directory/android/plop" "/tmp/strings_output_directory/android/plop.kt"

gh workflow run check_for_crowdin_updates.yml


https://crowdin.com/editor/session-crossplatform-strings/all?view=multilingual&languages=he,ha,hi,hu,id,ja,it,kn,km,ko,ku,kmr,lo,lt,lv,lg,mk,ms,mn,no,nenp,nb,nnno,ps,fa,pl,pt,ptbr,pain,ro&filter=basic&value=39

https://crowdin.com/editor/session-crossplatform-strings/all?view=multilingual&languages=ro,ru,sr,srcs,sh,silk,sk,sl,es,sw,es419,sv,tl,ta,te,th,uk,tr,urin,uz,vi,cy,xh&filter=basic&value=39

https://crowdin.com/editor/session-crossplatform-strings/all?view=multilingual&languages=fr,af,ar,be,bg,ca,cs,da,de,el,eu,fi,hy,ka,nl,sq,zhcn,zhtw,gl,bn,hr,et,az,my,eo,fil,lg,ha,bal,ny&filter=basic&value=39

 